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
from app.schemas.onboarding import OnboardingResponse, OnboardingUpdate

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


@router.patch("/me/onboarding", response_model=ApiResponse[OnboardingResponse])
def update_my_onboarding(
    payload: OnboardingUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """온보딩 답변을 고친다. 보낸 필드만 갱신하고, 답변이 없으면 새로 만든다.

    **upsert인 이유**: 온보딩을 건너뛰고 가입했거나 구글로 바로 가입한 계정은
    행 자체가 없다(get_my_onboarding이 404를 주는 상태). 그 사람들에게 404를
    돌려주면 "수정하려면 먼저 만드세요"가 되는데, 만들 화면이 따로 없다 —
    온보딩은 가입 전에만 지나가는 흐름이라 다시 갈 수 없다. 그래서 첫 PATCH가
    행을 만든다.

    POST /api/onboarding(익명 생성)과 달리 **user_id를 처음부터 채워서 만든다.**
    익명 행은 가입할 때 연결되지만, 이쪽은 이미 로그인한 사람이라 연결할 대상이
    분명하다. user_id는 unique라 한 사람에게 행이 둘 생길 일은 없다.
    """
    # exclude_unset이 핵심이다. 이게 없으면 안 보낸 필드가 기본값(None/[])으로
    # 들어와, 목표 학회 하나 고치려던 PATCH가 나머지 답변을 전부 지운다.
    changes = payload.model_dump(exclude_unset=True)

    # 리스트 컬럼은 JSONB지만 nullable=False다(models/onboarding.py). 명시적으로
    # null을 보내오면 DB에서 터져 500이 되므로, 그건 "안 보낸 것"과 같이 취급한다.
    # 비우고 싶으면 []를 보내면 된다. 반면 스칼라(venue 등)는 nullable이라
    # null이 "답을 지웠다"는 뜻으로 유효하다 — 그대로 통과시킨다.
    for name in ("purposes", "fields", "result_order"):
        if changes.get(name, ...) is None:
            changes.pop(name)

    profile = (
        db.query(OnboardingProfile).filter(OnboardingProfile.user_id == current_user.user_id).first()
    )
    if profile is None:
        profile = OnboardingProfile(user_id=current_user.user_id)
        db.add(profile)

    for name, value in changes.items():
        setattr(profile, name, value)

    db.commit()
    db.refresh(profile)
    return ApiResponse[OnboardingResponse](data=OnboardingResponse.model_validate(profile))
