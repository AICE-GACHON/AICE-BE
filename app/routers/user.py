from fastapi import APIRouter, Depends

from app.core.deps import get_current_user
from app.models.user import User
from app.schemas.auth import UserResponse
from app.schemas.common import ApiResponse

# 도메인: user (내 정보 조회/수정)
router = APIRouter(prefix="/api/user", tags=["user"])


@router.get("/me", response_model=ApiResponse[UserResponse])
def get_me(current_user: User = Depends(get_current_user)):
    return ApiResponse[UserResponse](data=UserResponse.model_validate(current_user))
