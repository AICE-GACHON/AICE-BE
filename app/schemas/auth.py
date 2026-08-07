import uuid

from typing import Annotated

from pydantic import AfterValidator, BaseModel, EmailStr, Field

from app.schemas.common import ORMBase, TimestampMixin


# bcrypt는 72**바이트**를 넘는 입력을 조용히 잘라낸다. 상한이 없으면 사용자가
# 긴 비밀번호를 쓰고도 실제로는 앞 72바이트만 보호받으면서 그 사실을 모른다.
# 72로 자르는 대신 넉넉한 상한에서 거부해, 잘림이 일어나지 않게 한다.
#
# ⚠️ **글자 수가 아니라 바이트 수를 센다.** max_length=72로 두면 한글 72자
# (=216바이트)가 통과하고 bcrypt가 앞 24자에서 잘라버린다 — 사용자는 긴
# 비밀번호를 쓴 줄 알지만 실제로 인증되는 건 앞 24자뿐이다.
_MAX_PASSWORD_BYTES = 72


def _within_bcrypt_limit(value: str) -> str:
    if len(value.encode("utf-8")) > _MAX_PASSWORD_BYTES:
        raise ValueError(f"비밀번호는 UTF-8 기준 {_MAX_PASSWORD_BYTES}바이트를 넘을 수 없습니다.")
    return value


NewPassword = Annotated[str, AfterValidator(_within_bcrypt_limit)]

# 로그인은 **가입 때보다 느슨해야 한다.** 위 규칙이 생기기 전에 만들어진 계정은
# 72바이트를 넘는 비밀번호를 가지고 있고, 그 값을 422로 막으면 (비밀번호 재설정
# 경로가 없으므로) 그 계정은 영영 로그인할 수 없게 된다. 긴 값을 그대로 받아도
# passlib이 72바이트에서 잘라 저장된 해시와 정상적으로 대조된다.
# 상한을 아예 없애지 않는 이유는 수 MB짜리 문자열을 bcrypt에 먹이지 않기 위해서다.
_MAX_LEGACY_PASSWORD_LEN = 1024

# JWT/구글 id_token 상한. 실제 토큰은 1KB 안팎이라 넉넉하다. 상한이 없으면
# 수 MB짜리 문자열을 서명 검증기에 그대로 먹일 수 있다.
_MAX_TOKEN_LEN = 4096


class SignupRequest(BaseModel):
    email: EmailStr
    password: NewPassword = Field(min_length=8)
    nickname: str = Field(min_length=1, max_length=50)
    # 서비스가 OpenReview 코퍼스 기반이라 가입 시점에 필수로 받는다.
    openreview_id: str = Field(min_length=1, max_length=100)
    # 회원가입 전 POST /api/onboarding으로 익명 저장해둔 답변을 이 계정에 연결한다.
    # 없거나 잘못된 값이어도 가입 자체는 막지 않는다 (부가 기능일 뿐).
    onboarding_id: uuid.UUID | None = None
    # SIGNUP_INVITE_CODE가 설정된 배포에서만 필요하다. 비어 있으면(개발 기본값)
    # 이 필드는 무시된다. 길이 상한은 비교 전에 거대한 문자열을 받지 않기 위한 것.
    invite_code: str | None = Field(default=None, max_length=200)


class LoginRequest(BaseModel):
    email: EmailStr
    # 로그인에는 min_length를 걸지 않는다 — 짧은 값을 422로 돌려주면 "이 길이는
    # 아예 후보가 아니다"를 알려주는 셈이고, 어차피 비밀번호가 틀려서 401이 된다.
    password: str = Field(max_length=_MAX_LEGACY_PASSWORD_LEN)


class GoogleLoginRequest(BaseModel):
    """프론트(Google Identity Services 등)가 발급받은 id_token을 그대로 전달한다.

    처음 구글로 가입하는 경우에만 openreview_id가 필요하다 (이미 연동된 계정이거나
    같은 이메일의 기존 계정에 연동되는 경우는 필요 없음).
    """
    id_token: str = Field(max_length=_MAX_TOKEN_LEN)
    openreview_id: str | None = Field(default=None, min_length=1, max_length=100)
    # openreview_id와 같은 조건이다 — **처음 구글로 가입할 때만** 필요하다.
    # 이미 있는 계정의 로그인/연동에는 요구하지 않는다 (초대받아 가입한 사람이
    # 로그인할 때마다 코드를 다시 입력해야 한다면 그건 초대가 아니라 비밀번호다).
    invite_code: str | None = Field(default=None, max_length=200)


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
