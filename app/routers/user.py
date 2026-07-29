from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.deps import get_current_user
from app.database import get_db
from app.models.onboarding import OnboardingProfile
from app.models.user import User
from app.schemas.auth import UserResponse, UserUpdateRequest
from app.schemas.common import ApiResponse, Message
from app.schemas.onboarding import OnboardingResponse

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
    """nickname/openreview_id 중 보낸 필드만 갱신한다 (둘 다 선택)."""
    if payload.nickname is not None:
        current_user.nickname = payload.nickname
    if payload.openreview_id is not None:
        current_user.openreview_id = payload.openreview_id
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


@router.get("/me/onboarding", response_model=ApiResponse[OnboardingResponse])
def get_my_onboarding(
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    """마이페이지에서 온보딩 답변을 다시 조회할 때 쓴다 (회원가입 시 연결된 것)."""
    profile = (
        db.query(OnboardingProfile).filter(OnboardingProfile.user_id == current_user.user_id).first()
    )
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="온보딩 답변이 없습니다.")
    return ApiResponse[OnboardingResponse](data=OnboardingResponse.model_validate(profile))
