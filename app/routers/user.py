from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.deps import get_current_user
from app.database import get_db
from app.models.user import User
from app.schemas.auth import UserResponse, UserUpdateRequest
from app.schemas.common import ApiResponse, Message

# 도메인: user (내 정보 조회/수정)
router = APIRouter(prefix="/api/user", tags=["user"])


@router.get("/me", response_model=ApiResponse[UserResponse])
def get_me(current_user: User = Depends(get_current_user)):
    return ApiResponse[UserResponse](data=UserResponse.model_validate(current_user))


@router.patch("/me", response_model=ApiResponse[UserResponse])
def update_me(
    payload: UserUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    current_user.nickname = payload.nickname
    db.commit()
    db.refresh(current_user)
    return ApiResponse[UserResponse](data=UserResponse.model_validate(current_user))


@router.delete("/me", response_model=ApiResponse[Message])
def delete_me(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """회원 탈퇴. submissions/review_predictions/similar_paper_matches는 FK
    ON DELETE CASCADE로 함께 삭제된다 (alembic/versions/0001_initial_tables.py).
    """
    db.delete(current_user)
    db.commit()
    return ApiResponse[Message](data=Message(message="회원 탈퇴가 완료되었습니다."))
