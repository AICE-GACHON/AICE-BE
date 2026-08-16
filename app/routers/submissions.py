"""내 논문 초안 업로드/조회 + 그 초안에 대한 분석 요청/조회.

분석 경로를 submission 하위에 둔 이유: 분석은 항상 "특정 초안 하나에 대한"
작업이라 submission_id가 경로에 있는 편이 소유권 검증과 폴링 모두에 자연스럽다.

**여기에는 HTTP 관심사만 둔다** — 경로, 인증, rate limit, 응답 모델, 백그라운드
작업 등록. 무엇을 논문으로 받아줄지(용량·페이지·길이 상한)와 분석 작업의 생애주기는
app/services/submissions.py와 app/services/analysis.py가 소유한다.
"""
import uuid

from fastapi import (
    APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, Response,
    UploadFile, status)
from sqlalchemy.orm import Session

from app.core.deps import get_current_user
from app.core.rate_limit import limiter, user_or_ip
from app.database import get_db
from app.models.user import User
from app.schemas.analysis import AnalysisResponse, AnalysisStartResponse
from app.schemas.common import ApiResponse
from app.schemas.share import ShareLinkResponse
from app.schemas.submission import (
    SimilarPaperMatchResponse, SubmissionResponse, SubmissionSummary)
from app.services import shares as share_service
from app.services import submissions as submission_service
from app.services.analysis import (
    expire_stale, latest_prediction, matches_for, run_analysis, start_prediction)

router = APIRouter(prefix="/api/submissions", tags=["submissions"])

# 로그인해야 부를 수 있지만 **호출마다 돈이 나가는** 두 엔드포인트의 상한.
# 인증만으로는 예산을 지킬 수 없다 — 계정 하나만 만들면 무한히 부를 수 있기
# 때문이다. `/story`에 이미 같은 이유로 상한이 걸려 있다(routers/corpus.py).
#
# 키가 IP가 아니라 **계정**인 이유는 rate_limit.user_or_ip 주석 참고.
#
# 업로드 20/hour: 업로드 1회는 PDF 추출 LLM 호출(정제 ~$0.001, 스캔본이면 비전
# 2페이지)이 붙고 20MB가 DB에 남는다. 한 사람이 논문 20편을 한 시간에 올릴 일은
# 없고, 스크립트로 훑는 경우는 여기서 멈춘다.
#
# 분석 10/hour: 1회가 최대 약 $0.36(60p 재정렬 + 종합)이다. 계정당 시간당 $3.6이
# 상한이 되고, 정상 사용(초안 하나를 고쳐가며 몇 번 돌려보기)은 넉넉히 들어간다.
_UPLOAD_LIMIT = "20/hour"
_ANALYSIS_LIMIT = "10/hour"


# ------------------------------------------------------------------ 초안 CRUD

@router.post(
    "/pdf", response_model=ApiResponse[SubmissionResponse], status_code=status.HTTP_201_CREATED)
@limiter.limit(_UPLOAD_LIMIT, key_func=user_or_ip)
async def create_submission_from_pdf(
    request: Request,
    pdf: UploadFile = File(...),
    title: str = Form(""),
    abstract: str = Form(""),
    field: str | None = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """내 논문을 PDF로 올린다. **유일한 입력 경로다.**

    텍스트 붙여넣기 경로는 없다. 2단계 LLM 재정렬이 입력 논문의 본문과 참고문헌까지
    봐야 하는데, 제목·초록만으로는 그걸 줄 수 없기 때문이다 — 텍스트 입력을 허용하면
    같은 API가 전혀 다른 품질의 결과를 내면서 그 사실이 드러나지 않는다.

    title/abstract를 비워 보내면 PDF에서 추출한다. 사용자가 직접 채워 보내면 그 값이
    우선한다(추출이 어긋났을 때의 교정 경로).

    검증 규칙과 거부 사유는 app/services/submissions.py를 보세요.
    """
    submission = await submission_service.create_from_pdf(
        db, current_user, pdf, title=title, abstract=abstract, field=field)
    return ApiResponse[SubmissionResponse](
        data=SubmissionResponse.model_validate(submission))


@router.get("", response_model=ApiResponse[list[SubmissionSummary]])
def list_submissions(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """내 초안 목록 (최신순). 본문은 싣지 않으니 상세는 개별 조회로 가져오세요."""
    rows = submission_service.list_for_user(db, current_user)
    return ApiResponse[list[SubmissionSummary]](
        data=[SubmissionSummary.model_validate(r) for r in rows])


@router.get("/{submission_id}", response_model=ApiResponse[SubmissionResponse])
def get_submission(
    submission_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    submission = submission_service.owned_submission(db, submission_id, current_user)
    return ApiResponse[SubmissionResponse](
        data=SubmissionResponse.model_validate(submission))


@router.delete("/{submission_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_submission(
    submission_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """초안과 그에 딸린 분석 결과를 함께 지웁니다 (FK ON DELETE CASCADE)."""
    submission = submission_service.owned_submission(db, submission_id, current_user)
    submission_service.delete(db, submission)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# -------------------------------------------------------------------- 분석

@router.post("/{submission_id}/analysis",
             response_model=ApiResponse[AnalysisStartResponse],
             status_code=status.HTTP_202_ACCEPTED)
@limiter.limit(_ANALYSIS_LIMIT, key_func=user_or_ip)
def start_analysis(
    request: Request,
    submission_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """분석을 시작한다. 결과를 기다리지 않고 202로 즉시 돌아온다.

    analyze() 한 번에 임베딩 로드 + 검색 + 집계가 들어가 수 초~수십 초 걸리기 때문에
    동기로 응답하지 않습니다. 프론트는 202를 받은 뒤 GET으로 폴링하세요.

    이미 진행 중인 분석이 있으면 새로 만들지 않고 그것을 돌려줍니다.

    ⚠️ **계정 기준 시간당 10회로 제한됩니다.** 1회가 최대 약 $0.36이라, 인증만으로는
    예산을 지킬 수 없습니다 (계정 하나로 무한히 부를 수 있으므로).
    """
    submission_service.owned_submission(db, submission_id, current_user)

    prediction, created = start_prediction(db, submission_id)
    if created:
        # 응답을 보낸 뒤 실행된다. 요청 세션은 그때 닫혀 있으므로 id만 넘긴다.
        # 이미 돌고 있는 작업(created=False)에는 새로 등록하지 않는다 — 같은
        # 분석이 두 번 돌면 비용도 두 배다.
        background_tasks.add_task(run_analysis, prediction.prediction_id)
    return ApiResponse[AnalysisStartResponse](
        data=AnalysisStartResponse.model_validate(prediction))


@router.get("/{submission_id}/analysis", response_model=ApiResponse[AnalysisResponse])
def get_analysis(
    submission_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """가장 최근 분석의 상태와 결과를 돌려준다 (폴링 대상).

    status가 done이면 report에 분석 결과 전체가, failed면 error에 사유가 들어갑니다.
    report를 화면에 옮길 때 주의할 점은 docs/DEVELOPMENT.md §6을 꼭 읽어주세요 —
    특히 confidence.level이 weak이면 경고 없이 결과만 보여주면 안 됩니다.
    """
    submission_service.owned_submission(db, submission_id, current_user)
    expire_stale(db, submission_id)

    prediction = latest_prediction(db, submission_id)
    if prediction is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="아직 분석을 시작하지 않았습니다.")

    data = AnalysisResponse.model_validate(prediction)
    data.matches = [SimilarPaperMatchResponse.model_validate(m)
                    for m in matches_for(db, prediction.prediction_id)]
    return ApiResponse[AnalysisResponse](data=data)


# -------------------------------------------------------------------- 공유

@router.post("/{submission_id}/share", response_model=ApiResponse[ShareLinkResponse])
def create_share_link(
    submission_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """공개 공유 링크를 발급한다 (이미 있으면 그것을 돌려준다).

    발급된 토큰을 아는 사람은 **로그인 없이** 분석 결과를 봅니다 — 이 호출이 곧
    공개 결정이므로 소유자만 부를 수 있고, 분석이 끝난 뒤에만 가능합니다.

    201이 아니라 200인 이유: 두 번째 호출부터는 아무것도 만들지 않고 기존 링크를
    그대로 돌려주기 때문입니다(멱등). 새 링크를 원하면 DELETE 후 다시 부르세요.
    """
    submission = submission_service.owned_submission(db, submission_id, current_user)
    share = share_service.issue(db, submission)
    return ApiResponse[ShareLinkResponse](data=ShareLinkResponse(
        token=share.token, url=share_service.share_url(share.token)))


@router.delete("/{submission_id}/share", status_code=status.HTTP_204_NO_CONTENT)
def revoke_share_link(
    submission_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """공유를 폐기한다. 그 뒤로 해당 토큰은 404가 됩니다.

    폐기할 공유가 없어도 204입니다 — 폐기는 멱등해야 하고, "이미 폐기됨"과 "방금
    폐기함"을 구분해 봐야 화면이 할 일이 같습니다.
    """
    submission_service.owned_submission(db, submission_id, current_user)
    share_service.revoke(db, submission_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
