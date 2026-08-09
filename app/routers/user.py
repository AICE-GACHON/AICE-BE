from fastapi import APIRouter, Body, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.deps import get_current_user
from app.core.security import hash_password, verify_password
from app.database import get_db
from app.models.onboarding import OnboardingProfile
from app.models.user import User
from app.schemas.auth import AccountDeleteRequest, UserResponse, UserUpdateRequest
from app.schemas.common import ApiResponse, Message
from app.schemas.onboarding import OnboardingResponse

# 도메인: user (내 정보 조회/수정/탈퇴)
router = APIRouter(prefix="/api/user", tags=["user"])

_duplicate_openreview_id = HTTPException(
    status_code=status.HTTP_409_CONFLICT, detail="이미 사용 중인 OpenReview ID입니다."
)
_wrong_password = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED, detail="비밀번호가 일치하지 않습니다."
)


@router.get("/me", response_model=ApiResponse[UserResponse])
def get_me(current_user: User = Depends(get_current_user)):
    return ApiResponse[UserResponse](data=UserResponse.model_validate(current_user))


@router.patch("/me", response_model=ApiResponse[UserResponse])
def update_me(
    payload: UserUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """nickname/openreview_id/비밀번호 중 보낸 것만 갱신한다 (전부 선택).

    비밀번호를 바꾸면 token_version을 올려 기존 refresh_token을 전부 폐기한다.
    비밀번호가 바뀌었다는 건 보통 "다른 기기에서 내보내고 싶다"는 뜻이고, 유출된
    비밀번호로 이미 발급된 refresh_token이 살아 있으면 바꾼 의미가 없다.
    (로그아웃과 같은 방식 — routers/auth.py logout 참고. access_token은 JWT라
    만료 전까지 유효한 한계도 동일하다.)
    """
    if payload.new_password is not None:
        if current_user.password_hash is None:
            # 구글 전용 계정은 바꿀 비밀번호가 없다. current_password를 무엇으로
            # 보내도 대조할 대상이 없으므로 400으로 분명히 알려준다.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="구글 로그인 전용 계정은 비밀번호를 설정할 수 없습니다.",
            )
        if not verify_password(payload.current_password, current_user.password_hash):
            raise _wrong_password
        current_user.password_hash = hash_password(payload.new_password)
        current_user.token_version += 1

    if payload.nickname is not None:
        current_user.nickname = payload.nickname
    if payload.openreview_id is not None:
        current_user.openreview_id = payload.openreview_id

    try:
        db.commit()
    except IntegrityError:
        # openreview_id는 unique다 (0006_unique_openreview_id). 사전 조회로 막으면
        # 동시 요청에서 빠져나가므로, 제약에 맡기고 여기서 409로 바꾼다.
        # 이게 없으면 남이 쓰는 ID를 넣었을 때 500이 난다.
        db.rollback()
        raise _duplicate_openreview_id
    db.refresh(current_user)
    return ApiResponse[UserResponse](data=UserResponse.model_validate(current_user))


@router.delete("/me", response_model=ApiResponse[Message])
def delete_me(
    payload: AccountDeleteRequest | None = Body(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """회원 탈퇴. submissions/review_predictions/similar_paper_matches는 FK
    ON DELETE CASCADE로 함께 삭제된다 (alembic/versions/0001_initial_tables.py).

    **비밀번호가 있는 계정은 비밀번호를 함께 보내야 한다.** 되돌릴 수 없는 삭제를
    access_token 하나로 실행하게 두면, 토큰이 유출됐을 때 계정이 통째로 사라진다.
    구글 전용 계정은 대조할 비밀번호가 없어 토큰만으로 진행한다.
    """
    if current_user.password_hash is not None:
        if payload is None or payload.password is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="탈퇴하려면 비밀번호를 입력해야 합니다.",
            )
        if not verify_password(payload.password, current_user.password_hash):
            raise _wrong_password

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
