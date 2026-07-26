import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.deps import get_current_user
from app.database import get_db
from app.models.submission import Submission
from app.models.user import User
from app.schemas.common import ApiResponse, Message
from app.schemas.submission import SubmissionCreate, SubmissionResponse

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
