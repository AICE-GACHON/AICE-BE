import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from jose import JWTError
from sqlalchemy.orm import Session

from app.core.deps import get_current_user
from app.core.security import (
    create_access_token, create_refresh_token, decode_token, hash_password, verify_password,
)
from app.database import get_db
from app.models.user import User
from app.schemas.auth import (
    LoginRequest, RefreshRequest, SignupRequest, TokenResponse, UserResponse,
)
from app.schemas.common import ApiResponse, Message

# 도메인: auth (회원가입 / 로그인)
router = APIRouter(prefix="/api/auth", tags=["auth"])

_invalid_refresh = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED, detail="refresh_token이 유효하지 않습니다."
)


@router.post("/signup", response_model=ApiResponse[UserResponse], status_code=status.HTTP_201_CREATED)
def signup(payload: SignupRequest, db: Session = Depends(get_db)):
    if db.query(User).filter(User.email == payload.email).first() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="이미 가입된 이메일입니다.")

    user = User(
        email=payload.email,
        password_hash=hash_password(payload.password),
        nickname=payload.nickname,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return ApiResponse[UserResponse](data=UserResponse.model_validate(user))


@router.post("/login", response_model=ApiResponse[TokenResponse])
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == payload.email).first()
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="이메일 또는 비밀번호가 올바르지 않습니다.",
        )

    access_token = create_access_token(subject=str(user.user_id))
    refresh_token = create_refresh_token(subject=str(user.user_id), version=user.token_version)
    return ApiResponse[TokenResponse](
        data=TokenResponse(access_token=access_token, refresh_token=refresh_token)
    )


@router.post("/refresh", response_model=ApiResponse[TokenResponse])
def refresh(payload: RefreshRequest, db: Session = Depends(get_db)):
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
