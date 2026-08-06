"""내 논문 초안 업로드/조회 + 그 초안에 대한 분석 요청/조회.

분석 경로를 submission 하위에 둔 이유: 분석은 항상 "특정 초안 하나에 대한"
작업이라 submission_id가 경로에 있는 편이 소유권 검증과 폴링 모두에 자연스럽다.
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import (
    APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, Response,
    UploadFile, status)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.deps import get_current_user
from app.core.rate_limit import limiter, user_or_ip
from app.database import get_db
from app.models.analysis import IN_PROGRESS, ReviewPrediction, SimilarPaperMatch
from app.models.submission import Submission
from app.models.user import User
from app.schemas.analysis import AnalysisResponse, AnalysisStartResponse
from app.schemas.common import ApiResponse
from app.schemas.submission import (
    SimilarPaperMatchResponse, SubmissionResponse, SubmissionSummary)
from app.services.analysis import run_analysis
from paper_assistant import extract_pdf_title_abstract, pdf_page_count
from paper_assistant.graph.llm import get_llm

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/submissions", tags=["submissions"])

# 이 시간이 지나도 pending/running이면 죽은 작업으로 본다. BackgroundTasks는
# 프로세스에 묶여 있어서, 서버가 재시작되면 진행 중이던 행이 영원히 running으로
# 남는다 — 정리하지 않으면 그 초안은 두 번 다시 분석할 수 없다.
STALE_AFTER = timedelta(minutes=15)

# 스캔본은 이제 비전 폴백이 살린다(extract.py). 여기까지 왔다는 것은 논문이 아니거나
# 비전도 읽지 못했다는 뜻이라, 원인을 스캔본으로 단정하지 않는다.
_PDF_EXTRACT_ERROR = (
    "PDF에서 제목과 초록을 추출할 수 없습니다. 논문 PDF가 맞는지 확인해 주세요.")
# Form 파라미터는 Pydantic Field(max_length=...) 검증을 안 타서 DB 컬럼 길이
# (String(300)/String(100))를 넘기면 500이 난다. 여기서 미리 걸러 422로 통일한다.
_MAX_TITLE_LEN = 300
_MAX_FIELD_LEN = 100
_MAX_ABSTRACT_LEN = 20_000  # Text 컬럼이라 DB 상한이 없다 — 여기서만 막힌다
_MAX_PDF_BYTES = 20 * 1024 * 1024  # 20MB — 논문 PDF치고 넉넉한 상한. 메모리에 통째로 읽으므로 무제한은 위험하다.
_PDF_CHUNK = 1024 * 1024           # 상한 검사를 위해 나눠 읽는 단위

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

# 페이지 상한이 둘인 이유: 경고와 거부는 다른 일이다.
#
# LLM 재정렬은 PDF를 통째로 넘기고 페이지당 약 2,970토큰이 든다. 60p면 분석 1회에
# 약 $0.36이라, 그 위는 논문이 아닐 개연성이 훨씬 높다고 보고 거부한다.
#
# WARN(15p)은 **서버가 강제하지 않는다.** page_count를 응답에 실어 보내면 프론트가
# "논문 PDF가 맞는지 확인해 주세요"를 띄우고, 사용자가 확인하면 그대로 진행한다.
# 경고는 UX이고 거부는 안전장치라, 한 곳에서 처리하려 하면 둘 다 어정쩡해진다.
_WARN_PAGE_COUNT = 15
_MAX_PAGE_COUNT = 60


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _owned_submission(db: Session, submission_id: uuid.UUID, user: User) -> Submission:
    """내 초안일 때만 돌려준다. 남의 초안은 존재 여부도 알리지 않도록 404로 통일."""
    submission = db.get(Submission, submission_id)
    if submission is None or submission.user_id != user.user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="논문 초안을 찾을 수 없습니다.")
    return submission


def _latest_prediction(db: Session, submission_id: uuid.UUID) -> ReviewPrediction | None:
    return db.scalars(
        select(ReviewPrediction)
        .where(ReviewPrediction.submission_id == submission_id)
        .order_by(ReviewPrediction.created_at.desc())
        .limit(1)
    ).first()


def _active_prediction(db: Session, submission_id: uuid.UUID) -> ReviewPrediction | None:
    return db.scalars(
        select(ReviewPrediction)
        .where(ReviewPrediction.submission_id == submission_id,
               ReviewPrediction.status.in_(IN_PROGRESS))
    ).first()


def _expire_stale(db: Session, submission_id: uuid.UUID) -> None:
    """오래 매달려 있는 분석을 failed로 내린다 (부분 유니크 인덱스 슬롯도 함께 풀린다)."""
    active = _active_prediction(db, submission_id)
    if active is None or active.created_at > _now() - STALE_AFTER:
        return
    active.status = "failed"
    active.error = "분석이 완료되지 않은 채 중단되었습니다 (서버 재시작 등). 다시 시도해 주세요."
    active.completed_at = _now()
    db.commit()


# ------------------------------------------------------------------ 초안 CRUD

async def _read_capped(upload: UploadFile, max_bytes: int) -> bytes:
    """상한까지만 읽고, 넘으면 즉시 끊는다.

    예전에는 `await upload.read()`로 통째로 읽은 **뒤에** 길이를 봤다. 그러면
    거부할 파일도 일단 전부 메모리에 올라간다 — 로그인한 사용자 한 명이 수 GB짜리
    본문을 보내면 상한 검사에 도달하기 전에 서버가 죽는다. 검사가 읽기보다
    먼저여야 의미가 있다.

    한 청크를 더 읽어보는 이유: 정확히 max_bytes에서 멈추면 "딱 상한"과 "상한
    초과"를 구분할 수 없다.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(_PDF_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(status_code=413, detail="PDF 용량이 너무 큽니다 (20MB 초과).")
        chunks.append(chunk)
    return b"".join(chunks)


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

    ⚠️ **추출에 실패하면 422로 거부한다.** 스캔본이나 옛 조판 PDF는 어떤 추출기로도
    복원이 어렵고(pdf/extract.py 주석), 제목·초록이 없으면 1단계 임베딩 자체가
    불가능하다. 사용자가 고칠 기회를 주기보다 명확히 거부하는 쪽을 택했다.
    """
    is_pdf = pdf.content_type == "application/pdf" or (pdf.filename or "").lower().endswith(".pdf")
    if not is_pdf:
        raise HTTPException(status_code=422, detail="PDF 파일만 업로드할 수 있습니다.")

    if len(title) > _MAX_TITLE_LEN or (field is not None and len(field) > _MAX_FIELD_LEN):
        raise HTTPException(status_code=422, detail="title/field 길이가 너무 깁니다.")

    # abstract는 Text 컬럼이라 DB가 막아주지 않는다. 상한이 없으면 초록 자리에
    # 수 MB짜리 문자열을 넣어 행을 부풀릴 수 있고, 그 값은 그대로 임베딩·LLM
    # 입력으로 흘러간다. 논문 초록은 길어야 수천 자다.
    if len(abstract) > _MAX_ABSTRACT_LEN:
        raise HTTPException(status_code=422, detail="abstract 길이가 너무 깁니다.")

    pdf_bytes = await _read_capped(pdf, _MAX_PDF_BYTES)

    # 페이지 수를 **추출보다 먼저** 본다 — 거부할 문서에 파싱 비용을 쓰지 않는다.
    # 여기서 PDF가 열리지 않으면 손상된 파일이므로 추출도 어차피 실패한다.
    try:
        pages = pdf_page_count(pdf_bytes)
    except Exception:
        log.exception("PDF 열기 실패")
        raise HTTPException(status_code=422, detail="PDF 파일을 열 수 없습니다. 손상된 파일일 수 있습니다.")

    if pages > _MAX_PAGE_COUNT:
        raise HTTPException(
            status_code=422,
            detail=f"{pages}페이지 문서입니다. 논문 PDF만 분석할 수 있습니다 "
                   f"({_MAX_PAGE_COUNT}페이지 이하).")

    if not title or not abstract:
        try:
            # llm을 넘기는 이유는 **스캔본**이다. 텍스트 추출이 실패하면 앞 2페이지를
            # 그림으로 렌더해 Haiku 비전이 읽는다 (extract.py). 텍스트가 멀쩡하면
            # 정제 호출 한 번(~$0.001)에 그치고, LLM이 꺼져 있으면 예전과 동일하다.
            extracted_title, extracted_abstract = extract_pdf_title_abstract(
                pdf_bytes, llm=get_llm())
        except Exception:
            log.exception("PDF 추출 실패")
            raise HTTPException(status_code=422, detail=_PDF_EXTRACT_ERROR)
        # 추출값도 상한을 넘을 수 있다(조판이 이상한 PDF에서 초록 경계를 놓치면
        # 본문 전체가 딸려온다). 사용자가 보낸 값이 아니라 우리 추출기의 실수이므로
        # 거부하지 말고 자른다 — 임베딩은 어차피 앞부분만 쓴다.
        title = title or extracted_title[:_MAX_TITLE_LEN]
        abstract = abstract or extracted_abstract[:_MAX_ABSTRACT_LEN]

    if not title or not abstract:
        raise HTTPException(status_code=422, detail=_PDF_EXTRACT_ERROR)

    submission = Submission(
        user_id=current_user.user_id, title=title, abstract=abstract, content=None,
        field=field, pdf_bytes=pdf_bytes, page_count=pages)
    db.add(submission)
    db.commit()
    db.refresh(submission)
    return ApiResponse[SubmissionResponse](
        data=SubmissionResponse.model_validate(submission))


@router.get("", response_model=ApiResponse[list[SubmissionSummary]])
def list_submissions(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """내 초안 목록 (최신순). 본문은 싣지 않으니 상세는 개별 조회로 가져오세요."""
    # created_at이 같을 수 있다 (server_default now()는 트랜잭션 시각이라 한
    # 트랜잭션에서 만든 행끼리는 동률이다). submission_id로 순서를 확정한다.
    rows = db.scalars(
        select(Submission)
        .where(Submission.user_id == current_user.user_id)
        .order_by(Submission.created_at.desc(), Submission.submission_id.desc())
    ).all()
    return ApiResponse[list[SubmissionSummary]](
        data=[SubmissionSummary.model_validate(r) for r in rows])


@router.get("/{submission_id}", response_model=ApiResponse[SubmissionResponse])
def get_submission(
    submission_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    submission = _owned_submission(db, submission_id, current_user)
    return ApiResponse[SubmissionResponse](
        data=SubmissionResponse.model_validate(submission))


@router.delete("/{submission_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_submission(
    submission_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """초안과 그에 딸린 분석 결과를 함께 지웁니다 (FK ON DELETE CASCADE)."""
    submission = _owned_submission(db, submission_id, current_user)
    db.delete(submission)
    db.commit()
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

    이미 진행 중인 분석이 있으면 새로 만들지 않고 그것을 돌려줍니다. 동시 요청
    경합은 DB의 부분 유니크 인덱스가 막고, 그때도 진행 중인 것을 돌려줍니다.

    ⚠️ **계정 기준 시간당 10회로 제한됩니다.** 1회가 최대 약 $0.36이라, 인증만으로는
    예산을 지킬 수 없습니다 (계정 하나로 무한히 부를 수 있으므로).
    """
    _owned_submission(db, submission_id, current_user)
    _expire_stale(db, submission_id)

    existing = _active_prediction(db, submission_id)
    if existing is not None:
        return ApiResponse[AnalysisStartResponse](
            data=AnalysisStartResponse.model_validate(existing))

    prediction = ReviewPrediction(submission_id=submission_id, status="pending")
    db.add(prediction)
    try:
        db.commit()
    except IntegrityError:
        # 같은 순간에 들어온 다른 요청이 먼저 만들었다 → 그 행을 돌려준다.
        db.rollback()
        existing = _active_prediction(db, submission_id)
        if existing is None:
            raise
        return ApiResponse[AnalysisStartResponse](
            data=AnalysisStartResponse.model_validate(existing))

    db.refresh(prediction)
    # 응답을 보낸 뒤 실행된다. 요청 세션은 그때 닫혀 있으므로 id만 넘긴다.
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
    _owned_submission(db, submission_id, current_user)
    _expire_stale(db, submission_id)

    prediction = _latest_prediction(db, submission_id)
    if prediction is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="아직 분석을 시작하지 않았습니다.")

    # 선정된 논문이 먼저, 그 안에서 llm_rank 순. 나머지 후보는 검색 순위 순으로
    # 뒤에 붙는다 — 프론트가 앞에서부터 잘라 써도 보여줄 것부터 나오게 한다.
    matches = db.scalars(
        select(SimilarPaperMatch)
        .where(SimilarPaperMatch.prediction_id == prediction.prediction_id)
        .order_by(SimilarPaperMatch.selected.desc(),
                  SimilarPaperMatch.llm_rank.nulls_last(),
                  SimilarPaperMatch.rank)
    ).all()

    data = AnalysisResponse.model_validate(prediction)
    data.matches = [SimilarPaperMatchResponse.model_validate(m) for m in matches]
    return ApiResponse[AnalysisResponse](data=data)
