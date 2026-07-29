import logging
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.core.deps import get_current_user
from app.database import get_db
from app.models.submission import Submission
from app.models.user import User
from app.schemas.common import ApiResponse, Message
from app.schemas.submission import SubmissionCreate, SubmissionResponse
from paper_assistant import extract_pdf_title_abstract

log = logging.getLogger(__name__)

_PDF_EXTRACT_ERROR = "PDF에서 텍스트를 추출할 수 없습니다."
# JSON 경로(SubmissionCreate)는 Pydantic Field(max_length=...)로 걸러지지만, 이
# 엔드포인트는 Form 파라미터라 그 검증을 안 타서 DB 컬럼 길이(String(300)/String(100))를
# 넘기면 500이 난다. 여기서 미리 걸러 422로 통일한다.
_MAX_TITLE_LEN = 300
_MAX_FIELD_LEN = 100
_MAX_PDF_BYTES = 20 * 1024 * 1024  # 20MB — 논문 PDF치고 넉넉한 상한. 메모리에 통째로 읽으므로 무제한은 위험하다.

# 도메인: submission (사용자가 올린 내 논문 초안 업로드/조회)
router = APIRouter(prefix="/api/submissions", tags=["submission"])


def _get_owned_submission(db: Session, submission_id: uuid.UUID, user: User) -> Submission:
    """내 초안일 때만 돌려준다. 남의 초안은 존재 여부도 알리지 않도록 404로 통일한다."""
    submission = db.get(Submission, submission_id)
    if submission is None or submission.user_id != user.user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="논문 초안을 찾을 수 없습니다.")
    return submission


@router.post("", response_model=ApiResponse[SubmissionResponse], status_code=status.HTTP_201_CREATED)
def create_submission(
    payload: SubmissionCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    submission = Submission(
        user_id=current_user.user_id,
        title=payload.title,
        abstract=payload.abstract,
        content=payload.content,
        field=payload.field,
    )
    db.add(submission)
    db.commit()
    db.refresh(submission)
    return ApiResponse[SubmissionResponse](data=SubmissionResponse.model_validate(submission))


@router.post(
    "/pdf", response_model=ApiResponse[SubmissionResponse], status_code=status.HTTP_201_CREATED
)
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
        user_id=current_user.user_id,
        title=title,
        abstract=abstract,
        content=None,
        field=field,
    )
    db.add(submission)
    db.commit()
    db.refresh(submission)
    return ApiResponse[SubmissionResponse](data=SubmissionResponse.model_validate(submission))


@router.get("", response_model=ApiResponse[list[SubmissionResponse]])
def list_submissions(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    submissions = (
        db.query(Submission)
        .filter(Submission.user_id == current_user.user_id)
        .order_by(Submission.created_at.desc())
        .all()
    )
    return ApiResponse[list[SubmissionResponse]](
        data=[SubmissionResponse.model_validate(s) for s in submissions]
    )


@router.get("/{submission_id}", response_model=ApiResponse[SubmissionResponse])
def get_submission(
    submission_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    submission = _get_owned_submission(db, submission_id, current_user)
    return ApiResponse[SubmissionResponse](data=SubmissionResponse.model_validate(submission))


@router.delete("/{submission_id}", response_model=ApiResponse[Message])
def delete_submission(
    submission_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """초안을 삭제한다. review_predictions/similar_paper_matches는 FK
    ON DELETE CASCADE로 함께 삭제된다.
    """
    submission = _get_owned_submission(db, submission_id, current_user)
    db.delete(submission)
    db.commit()
    return ApiResponse[Message](data=Message(message="초안이 삭제되었습니다."))
