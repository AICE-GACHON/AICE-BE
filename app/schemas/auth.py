import uuid

from pydantic import BaseModel, EmailStr, Field

from app.schemas.common import ORMBase, TimestampMixin


# bcrypt는 72바이트를 넘는 입력을 **조용히 잘라낸다.** 상한이 없으면 사용자가
# 긴 비밀번호를 쓰고도 실제로는 앞 72바이트만 보호받으면서 그 사실을 모른다.
# 72로 자르는 대신 넉넉한 상한에서 거부해, 잘림이 일어나지 않게 한다.
_MAX_PASSWORD_LEN = 72

# JWT/구글 id_token 상한. 실제 토큰은 1KB 안팎이라 넉넉하다. 상한이 없으면
# 수 MB짜리 문자열을 서명 검증기에 그대로 먹일 수 있다.
_MAX_TOKEN_LEN = 4096


class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=_MAX_PASSWORD_LEN)
    nickname: str = Field(min_length=1, max_length=50)
    # 서비스가 OpenReview 코퍼스 기반이라 가입 시점에 필수로 받는다.
    openreview_id: str = Field(min_length=1, max_length=100)
    # 회원가입 전 POST /api/onboarding으로 익명 저장해둔 답변을 이 계정에 연결한다.
    # 없거나 잘못된 값이어도 가입 자체는 막지 않는다 (부가 기능일 뿐).
    onboarding_id: uuid.UUID | None = None


class LoginRequest(BaseModel):
    email: EmailStr
    # 로그인에는 min_length를 걸지 않는다 — 짧은 값을 422로 돌려주면 "이 길이는
    # 아예 후보가 아니다"를 알려주는 셈이고, 어차피 비밀번호가 틀려서 401이 된다.
    password: str = Field(max_length=_MAX_PASSWORD_LEN)


class GoogleLoginRequest(BaseModel):
    """프론트(Google Identity Services 등)가 발급받은 id_token을 그대로 전달한다.

    처음 구글로 가입하는 경우에만 openreview_id가 필요하다 (이미 연동된 계정이거나
    같은 이메일의 기존 계정에 연동되는 경우는 필요 없음).
    """
    id_token: str = Field(max_length=_MAX_TOKEN_LEN)
    openreview_id: str | None = Field(default=None, min_length=1, max_length=100)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(max_length=_MAX_TOKEN_LEN)


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
