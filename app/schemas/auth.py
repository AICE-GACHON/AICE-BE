import uuid

from pydantic import BaseModel, EmailStr, Field

from app.schemas.common import ORMBase, TimestampMixin


class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    nickname: str = Field(min_length=1, max_length=50)
    # 서비스가 OpenReview 코퍼스 기반이라 가입 시점에 필수로 받는다.
    openreview_id: str = Field(min_length=1, max_length=100)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class GoogleLoginRequest(BaseModel):
    """프론트(Google Identity Services 등)가 발급받은 id_token을 그대로 전달한다.

    처음 구글로 가입하는 경우에만 openreview_id가 필요하다 (이미 연동된 계정이거나
    같은 이메일의 기존 계정에 연동되는 경우는 필요 없음).
    """
    id_token: str
    openreview_id: str | None = Field(default=None, min_length=1, max_length=100)


class RefreshRequest(BaseModel):
    refresh_token: str


class UserUpdateRequest(BaseModel):
    nickname: str | None = Field(default=None, min_length=1, max_length=50)
    openreview_id: str | None = Field(default=None, min_length=1, max_length=100)


class UserResponse(ORMBase, TimestampMixin):
    user_id: uuid.UUID
    email: EmailStr
    nickname: str
    openreview_id: str
    google_linked: bool = Field(description="구글 계정이 연동돼 있는지")


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
