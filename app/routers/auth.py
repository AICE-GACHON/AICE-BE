import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from jose import JWTError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.deps import get_current_user
from app.core.google_oauth import verify_google_id_token
from app.core.rate_limit import limiter
from app.core.security import (
    create_access_token, create_refresh_token, decode_token, hash_password, verify_password,
)
from app.database import get_db
from app.models.onboarding import OnboardingProfile
from app.models.user import User
from app.schemas.auth import (
    GoogleLoginRequest, LoginRequest, RefreshRequest, SignupRequest, TokenResponse, UserResponse,
)
from app.schemas.common import ApiResponse, Message

# 도메인: auth (회원가입 / 로그인)
router = APIRouter(prefix="/api/auth", tags=["auth"])

_invalid_refresh = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED, detail="refresh_token이 유효하지 않습니다."
)
_duplicate_account = HTTPException(
    status_code=status.HTTP_409_CONFLICT, detail="이미 사용 중인 이메일 또는 OpenReview ID입니다."
)


@router.post("/signup", response_model=ApiResponse[UserResponse], status_code=status.HTTP_201_CREATED)
@limiter.limit("10/minute")
def signup(request: Request, payload: SignupRequest, db: Session = Depends(get_db)):
    if db.query(User).filter(User.email == payload.email).first() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="이미 가입된 이메일입니다.")

    user = User(
        email=payload.email,
        password_hash=hash_password(payload.password),
        nickname=payload.nickname,
        openreview_id=payload.openreview_id,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        # 이메일은 위에서 이미 체크했지만 동시 요청(race condition)이면 여기서
        # 걸릴 수 있고, openreview_id 중복은 사전 체크가 없어 여기서만 걸린다.
        db.rollback()
        raise _duplicate_account
    db.refresh(user)

    if payload.onboarding_id is not None:
        profile = db.get(OnboardingProfile, payload.onboarding_id)
        if profile is not None and profile.user_id is None:
            profile.user_id = user.user_id
            db.commit()
        # profile이 없거나 이미 다른 계정에 연결돼 있어도 회원가입은 그대로 성공시킨다
        # (온보딩 연결은 부가 기능이지, 가입을 막을 이유가 아니다)

    return ApiResponse[UserResponse](data=UserResponse.model_validate(user))


@router.post("/login", response_model=ApiResponse[TokenResponse])
@limiter.limit("10/minute")
def login(request: Request, payload: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == payload.email).first()
    # password_hash가 없으면 구글 전용 계정 — verify_password에 None을 넘기면
    # passlib이 에러를 던지므로 그 전에 걸러야 한다.
    if user is None or user.password_hash is None or not verify_password(
        payload.password, user.password_hash
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="이메일 또는 비밀번호가 올바르지 않습니다.",
        )

    access_token = create_access_token(subject=str(user.user_id))
    refresh_token = create_refresh_token(subject=str(user.user_id), version=user.token_version)
    return ApiResponse[TokenResponse](
        data=TokenResponse(access_token=access_token, refresh_token=refresh_token)
    )


@router.post("/google", response_model=ApiResponse[TokenResponse])
@limiter.limit("10/minute")
def google_login(request: Request, payload: GoogleLoginRequest, db: Session = Depends(get_db)):
    """구글 로그인/연동. 처음 보는 구글 계정이면 openreview_id가 필수다.

    매칭 우선순위: google_sub(이미 연동된 계정) → email(이메일로 가입한 기존
    계정에 이번에 연동) → 신규 가입. 이메일 계정에 연동될 때는 비밀번호 로그인도
    계속 가능하다 — google_sub만 채워질 뿐 password_hash는 그대로 둔다.
    """
    try:
        claims = verify_google_id_token(payload.id_token)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="구글 로그인에 실패했습니다.")

    google_sub = claims["sub"]
    email = claims.get("email")

    # email_verified가 false면 이 이메일이 실제로 그 구글 계정 소유인지 구글도
    # 보장하지 않는다는 뜻이다. 이걸 무시하고 이메일로 기존 계정에 자동 연동하면,
    # 검증 안 된 이메일만 들고 있는 공격자가 남의 email/password 계정을 가로챌 수
    # 있다 — 그래서 이메일 매칭/신규가입 둘 다 여기서 막는다.
    if email and not claims.get("email_verified", False):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="구글 계정의 이메일이 인증되지 않았습니다."
        )

    user = db.query(User).filter(User.google_sub == google_sub).first()

    if user is None and email:
        user = db.query(User).filter(User.email == email).first()
        if user is not None:
            user.google_sub = google_sub
            db.commit()

    if user is None:
        if not payload.openreview_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="처음 구글로 가입할 때는 openreview_id가 필요합니다.",
            )
        if not email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="구글 계정에 이메일이 없습니다."
            )
        user = User(
            email=email,
            password_hash=None,
            nickname=(claims.get("name") or email)[:50],
            google_sub=google_sub,
            openreview_id=payload.openreview_id,
        )
        db.add(user)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise _duplicate_account
        db.refresh(user)

    access_token = create_access_token(subject=str(user.user_id))
    refresh_token = create_refresh_token(subject=str(user.user_id), version=user.token_version)
    return ApiResponse[TokenResponse](
        data=TokenResponse(access_token=access_token, refresh_token=refresh_token)
    )


@router.post("/refresh", response_model=ApiResponse[TokenResponse])
@limiter.limit("20/minute")
def refresh(request: Request, payload: RefreshRequest, db: Session = Depends(get_db)):
    """refresh_token으로 access_token을 재발급한다 (refresh_token도 함께 회전).

    로그아웃된 사용자의 refresh_token은 User.token_version이 올라가 있어
    payload의 ver과 어긋나므로 여기서 걸러진다.
    """
    try:
        decoded = decode_token(payload.refresh_token)
    except JWTError:
        raise _invalid_refresh

    if decoded.get("type") != "refresh":
        raise _invalid_refresh

    try:
        user = db.get(User, uuid.UUID(decoded.get("sub")))
    except (ValueError, TypeError):
        raise _invalid_refresh

    if user is None or decoded.get("ver") != user.token_version:
        raise _invalid_refresh

    access_token = create_access_token(subject=str(user.user_id))
    new_refresh_token = create_refresh_token(subject=str(user.user_id), version=user.token_version)
    return ApiResponse[TokenResponse](
        data=TokenResponse(access_token=access_token, refresh_token=new_refresh_token)
    )


@router.post("/logout", response_model=ApiResponse[Message])
def logout(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """토큰을 폐기한다. JWT는 상태가 없어 발급된 access_token 자체는 만료 전까지
    유효하지만, token_version을 올려 이후 refresh_token 재발급을 막는다.
    """
    current_user.token_version += 1
    db.commit()
    return ApiResponse[Message](data=Message(message="로그아웃되었습니다."))
