from fastapi import APIRouter, Depends

from app.core.deps import get_current_user
from app.models.user import User
from app.schemas.auth import UserResponse

# 도메인: user (내 정보 조회/수정)
router = APIRouter(prefix="/api/user", tags=["user"])


@router.get("/me", response_model=UserResponse)
def get_me(current_user: User = Depends(get_current_user)):
    return current_user
