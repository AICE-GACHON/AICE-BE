from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.core.security import get_current_user
from app.database import get_db
from app.models.submission import Submission
from app.models.user import User
from app.schemas.submission import SubmissionCreate, SubmissionResponse

# 도메인: submission (사용자가 올린 내 논문 초안 업로드/조회)
router = APIRouter(prefix="/api/submissions", tags=["submission"])


@router.post("", response_model=SubmissionResponse, status_code=status.HTTP_201_CREATED)
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
    return submission
