import uuid

from typing import Annotated

from pydantic import AfterValidator, BaseModel, EmailStr, Field, field_validator, model_validator

from app.models.user import OPENREVIEW_ID_PENDING_PREFIX
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
    # 예전에는 가입 시점에 필수였다. **베타에서는 받지 않는다** — 지금 이 값을
    # 읽는 기능이 하나도 없는데(모델 주석: "나중에 쓸 신원 값") 가입 문턱만
    # 높이고 있었다. 안 보내면 서버가 고유한 자리표시자를 만들어 넣는다
    # (컬럼이 unique·not null이라 비워둘 수는 없다). 나중에 프로필 수정으로
    # 진짜 ID를 넣을 수 있다.
    openreview_id: str | None = Field(default=None, max_length=100)
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

    베타에서는 신규 가입에도 openreview_id를 요구하지 않는다 — 보내면 쓰고,
    없으면 서버가 자리표시자를 만든다 (app/routers/auth.py).
    """
    id_token: str = Field(max_length=_MAX_TOKEN_LEN)
    openreview_id: str | None = Field(default=None, min_length=1, max_length=100)
    # invite_code는 **처음 구글로 가입할 때만** 필요하다.
    # 이미 있는 계정의 로그인/연동에는 요구하지 않는다 (초대받아 가입한 사람이
    # 로그인할 때마다 코드를 다시 입력해야 한다면 그건 초대가 아니라 비밀번호다).
    invite_code: str | None = Field(default=None, max_length=200)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(max_length=_MAX_TOKEN_LEN)


class PasswordForgotRequest(BaseModel):
    """비밀번호 재설정 메일 요청. 계정이 없어도 응답은 같다(계정 존재 여부 노출 방지)."""
    email: EmailStr


class PasswordResetRequest(BaseModel):
    """메일로 받은 토큰으로 새 비밀번호를 정한다."""
    token: str = Field(max_length=_MAX_TOKEN_LEN)
    new_password: NewPassword = Field(min_length=8)


class UserUpdateRequest(BaseModel):
    """보낸 필드만 갱신한다 (전부 선택).

    비밀번호는 `current_password`와 `new_password`를 **함께** 보내야 바뀐다.
    현재 비밀번호를 확인하지 않으면, 탈취된 access_token 하나로 계정을 통째로
    빼앗을 수 있다 — 비밀번호를 바꿔버리면 원래 주인이 다시 들어올 수 없다.
    """
    nickname: str | None = Field(default=None, min_length=1, max_length=50)
    openreview_id: str | None = Field(default=None, min_length=1, max_length=100)
    # 기존 비밀번호는 로그인과 같은 이유로 느슨하게 받는다 — 72바이트 규칙이 생기기
    # 전에 만들어진 계정은 그보다 긴 값을 쓰고 있고, 422로 막으면 그 계정은 비밀번호를
    # 영영 바꿀 수 없게 된다. 새 비밀번호에만 NewPassword 규칙을 건다.
    current_password: str | None = Field(default=None, max_length=_MAX_LEGACY_PASSWORD_LEN)
    new_password: NewPassword | None = Field(default=None, min_length=8)

    @model_validator(mode="after")
    def _new_password_requires_current(self) -> "UserUpdateRequest":
        if self.new_password is not None and self.current_password is None:
            raise ValueError("비밀번호를 변경하려면 current_password가 필요합니다.")
        return self


class AccountDeleteRequest(BaseModel):
    """탈퇴 확인. 비밀번호가 있는 계정은 필수다 (구글 전용 계정은 없어도 된다).

    탈퇴는 되돌릴 수 없는데 access_token 하나로 실행되면 위험 대비 확인이 너무
    가볍다. 로그인과 같은 이유로 길이 규칙은 느슨하게 둔다.
    """
    password: str | None = Field(default=None, max_length=_MAX_LEGACY_PASSWORD_LEN)


class UserResponse(ORMBase, TimestampMixin):
    user_id: uuid.UUID
    email: EmailStr
    nickname: str
    # 아직 안 채운 계정은 **null이다.** DB에는 자리표시자가 들어 있지만
    # (models/user.py OPENREVIEW_ID_PENDING_PREFIX) 그건 unique·not null 제약을
    # 만족시키려는 사정일 뿐이라 밖으로 내보내지 않는다. 그대로 내보내면 화면에
    # `pending:9f3c8a1e-…`가 사용자 ID인 것처럼 찍힌다.
    openreview_id: str | None = Field(
        default=None, description="아직 연결하지 않았으면 null")
    google_linked: bool = Field(description="구글 계정이 연동돼 있는지")
    has_password: bool = Field(
        description="비밀번호로 로그인할 수 있는 계정인지. "
                    "false면 구글 전용이라 비밀번호 변경이 400이고, 탈퇴에 비밀번호가 필요 없다")

    @field_validator("openreview_id", mode="after")
    @classmethod
    def _hide_placeholder(cls, value: str | None) -> str | None:
        if value is not None and value.startswith(OPENREVIEW_ID_PENDING_PREFIX):
            return None
        return value


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
