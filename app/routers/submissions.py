"""내 논문 초안 업로드/조회 + 그 초안에 대한 분석 요청/조회.

분석 경로를 submission 하위에 둔 이유: 분석은 항상 "특정 초안 하나에 대한"
작업이라 submission_id가 경로에 있는 편이 소유권 검증과 폴링 모두에 자연스럽다.
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import (
    APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Response, UploadFile, status)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.deps import get_current_user
from app.database import get_db
from app.models.analysis import IN_PROGRESS, ReviewPrediction, SimilarPaperMatch
from app.models.submission import Submission
from app.models.user import User
from app.schemas.analysis import AnalysisResponse, AnalysisStartResponse
from app.schemas.common import ApiResponse
from app.schemas.submission import (
    SimilarPaperMatchResponse, SubmissionCreate, SubmissionResponse, SubmissionSummary)
from app.services.analysis import run_analysis
from paper_assistant import extract_pdf_title_abstract

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/submissions", tags=["submissions"])

# 이 시간이 지나도 pending/running이면 죽은 작업으로 본다. BackgroundTasks는
# 프로세스에 묶여 있어서, 서버가 재시작되면 진행 중이던 행이 영원히 running으로
# 남는다 — 정리하지 않으면 그 초안은 두 번 다시 분석할 수 없다.
STALE_AFTER = timedelta(minutes=15)

_PDF_EXTRACT_ERROR = "PDF에서 텍스트를 추출할 수 없습니다."
# JSON 경로(SubmissionCreate)는 Pydantic Field(max_length=...)로 걸러지지만, 이
# 엔드포인트는 Form 파라미터라 그 검증을 안 타서 DB 컬럼 길이(String(300)/String(100))를
# 넘기면 500이 난다. 여기서 미리 걸러 422로 통일한다.
_MAX_TITLE_LEN = 300
_MAX_FIELD_LEN = 100
_MAX_PDF_BYTES = 20 * 1024 * 1024  # 20MB — 논문 PDF치고 넉넉한 상한. 메모리에 통째로 읽으므로 무제한은 위험하다.


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

@router.post("", response_model=ApiResponse[SubmissionResponse],
             status_code=status.HTTP_201_CREATED)
def create_submission(
    payload: SubmissionCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    submission = Submission(user_id=current_user.user_id, **payload.model_dump())
    db.add(submission)
    db.commit()
    db.refresh(submission)
    return ApiResponse[SubmissionResponse](
        data=SubmissionResponse.model_validate(submission))


@router.post(
    "/pdf", response_model=ApiResponse[SubmissionResponse], status_code=status.HTTP_201_CREATED)
async def create_submission_from_pdf(
    pdf: UploadFile = File(...),
    title: str = Form(""),
    abstract: str = Form(""),
    field: str | None = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """PDF로 초안을 올린다. title/abstract가 비어 있으면 PDF에서 추출한다
    (paper_assistant.extract_pdf_title_abstract — analyze(pdf_bytes=...)가 내부에서
    쓰는 것과 같은 추출기, demo/server.py의 /api/analyze와 동일한 패턴).
    """
    is_pdf = pdf.content_type == "application/pdf" or (pdf.filename or "").lower().endswith(".pdf")
    if not is_pdf:
        raise HTTPException(status_code=422, detail=_PDF_EXTRACT_ERROR)

    if len(title) > _MAX_TITLE_LEN or (field is not None and len(field) > _MAX_FIELD_LEN):
        raise HTTPException(status_code=422, detail="title/field 길이가 너무 깁니다.")

    pdf_bytes = await pdf.read()
    if len(pdf_bytes) > _MAX_PDF_BYTES:
        raise HTTPException(status_code=422, detail="PDF 용량이 너무 큽니다 (20MB 초과).")

    if not title or not abstract:
        try:
            extracted_title, extracted_abstract = extract_pdf_title_abstract(pdf_bytes)
        except Exception:
            log.exception("PDF 추출 실패")
            raise HTTPException(status_code=422, detail=_PDF_EXTRACT_ERROR)
        title = title or extracted_title
        abstract = abstract or extracted_abstract

    if not title or not abstract:
        raise HTTPException(status_code=422, detail=_PDF_EXTRACT_ERROR)

    submission = Submission(
        user_id=current_user.user_id, title=title, abstract=abstract, content=None, field=field)
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
def start_analysis(
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
    report를 화면에 옮길 때 주의할 점은 docs/AI_파트_팀_공유.md §4를 꼭 읽어주세요 —
    특히 confidence.level이 weak이면 경고 없이 결과만 보여주면 안 됩니다.
    """
    _owned_submission(db, submission_id, current_user)
    _expire_stale(db, submission_id)

    prediction = _latest_prediction(db, submission_id)
    if prediction is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="아직 분석을 시작하지 않았습니다.")

    matches = db.scalars(
        select(SimilarPaperMatch)
        .where(SimilarPaperMatch.prediction_id == prediction.prediction_id)
        .order_by(SimilarPaperMatch.rank)
    ).all()

    data = AnalysisResponse.model_validate(prediction)
    data.matches = [SimilarPaperMatchResponse.model_validate(m) for m in matches]
    return ApiResponse[AnalysisResponse](data=data)
