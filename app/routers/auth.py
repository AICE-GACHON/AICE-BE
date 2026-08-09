import secrets
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from jose import JWTError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.deps import get_current_user
from app.core.google_oauth import verify_google_id_token
from app.core.mail import MailNotConfigured, deliver_password_reset
from app.core.rate_limit import limiter
from app.core.security import (
    create_access_token, create_password_reset_token, create_refresh_token, decode_token,
    dummy_verify, hash_password, verify_password,
)
from app.database import get_db
from app.models.onboarding import OnboardingProfile
from app.models.user import User
from app.schemas.auth import (
    GoogleLoginRequest, LoginRequest, PasswordForgotRequest, PasswordResetRequest, RefreshRequest,
    SignupRequest, TokenResponse, UserResponse,
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

# 분당 상한(10/minute)만 있으면 그 아래로 꾸준히 두드리는 무차별 대입은 통과한다 —
# IP 하나로 하루 14,400번이다. 시간 단위 상한을 겹쳐 그 창을 닫는다. 사람이 로그인을
# 한 시간에 30번 시도할 일은 없고, 오타로 몇 번 틀리는 정상 사용은 그대로 통과한다.
_LOGIN_HOURLY_LIMIT = "30/hour"


def _openreview_id_or_placeholder(given: str | None) -> str:
    """베타에서는 가입 시 OpenReview ID를 받지 않는다. 없으면 자리표시자를 만든다.

    컬럼이 `unique=True, nullable=False`라(app/models/user.py) 비워둘 수 없다.
    **모든 사용자에게 같은 값을 넣는 것은 답이 아니다** — 두 번째 가입자가
    IntegrityError로 거절된다. 그래서 계정마다 고유한 값을 만든다.

    `pending:` 접두사는 의도적이다. 진짜 OpenReview ID는 `~Name1` 꼴이라 섞이지
    않고, 나중에 "아직 안 채운 사람"을 한 번에 찾아낼 수 있다. 사용자는
    PATCH /api/user/me로 진짜 ID를 넣을 수 있다.
    """
    given = (given or "").strip()
    return given or f"pending:{uuid.uuid4()}"


def _require_invite(code: str | None) -> None:
    """SIGNUP_INVITE_CODE가 설정돼 있으면 신규 가입에 그 코드를 요구한다.

    **가입 경로가 둘이라는 것이 이 함수의 존재 이유다.** 이메일 가입만 막으면
    구글 버튼으로 그대로 뚫린다 — google_login()은 처음 보는 구글 계정을 그 자리에서
    새로 만든다. 그래서 두 곳이 같은 함수를 부른다 (docs/배포_계획.md D5).

    이 게이트가 지키는 것은 계정이 아니라 **예산**이다. 돈이 나가는 경로(분석,
    /story?refresh=true)가 전부 로그인 뒤에 있으므로, 가입이 잠기면 지출이
    초대 인원 안에 묶인다.

    비어 있으면(개발 기본값) 아무것도 하지 않는다. production에서 LLM이 켜져 있는데
    이 값이 비면 애초에 서버가 뜨지 않는다 (app/core/config.py).
    """
    expected = settings.SIGNUP_INVITE_CODE
    if not expected:
        return
    # compare_digest는 길이가 다르면 바로 False다 — 길이 자체는 어차피 숨겨지지
    # 않으므로 문제 없고, 같은 길이일 때의 내용 비교가 상수 시간인 것이 요점이다.
    if code is None or not secrets.compare_digest(code, expected):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="초대 코드가 필요합니다. 코드가 있다면 다시 확인해 주세요.",
        )


@router.post("/signup", response_model=ApiResponse[UserResponse], status_code=status.HTTP_201_CREATED)
@limiter.limit("10/minute")
def signup(request: Request, payload: SignupRequest, db: Session = Depends(get_db)):
    # DB를 건드리기 **전에** 검사한다. 뒤에 두면 초대받지 않은 사람도 409/201 차이로
    # 어떤 이메일이 이미 가입돼 있는지 알아낼 수 있다.
    _require_invite(payload.invite_code)

    if db.query(User).filter(User.email == payload.email).first() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="이미 가입된 이메일입니다.")

    user = User(
        email=payload.email,
        password_hash=hash_password(payload.password),
        nickname=payload.nickname,
        openreview_id=_openreview_id_or_placeholder(payload.openreview_id),
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
@limiter.limit(_LOGIN_HOURLY_LIMIT)
def login(request: Request, payload: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == payload.email).first()
    # password_hash가 없으면 구글 전용 계정 — verify_password에 None을 넘기면
    # passlib이 에러를 던지므로 그 전에 걸러야 한다.
    if user is None or user.password_hash is None:
        # 계정이 없거나 비밀번호가 없는 경우에도 bcrypt를 한 번 돌린다.
        #
        # 그냥 401을 던지면 응답이 즉시 돌아오고, 실제 계정일 때만 bcrypt 검증
        # (수십~수백 ms)이 걸린다. 이 차이는 네트워크 너머에서도 안정적으로 측정
        # 가능해서, 응답 메시지를 아무리 똑같이 맞춰도 **어떤 이메일이 가입돼
        # 있는지가 그대로 새어 나간다.** 이메일 목록은 그 자체로 표적 피싱과
        # 크리덴셜 스터핑의 입력이다.
        dummy_verify(payload.password)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="이메일 또는 비밀번호가 올바르지 않습니다.",
        )

    if not verify_password(payload.password, user.password_hash):
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
        # 🔴 여기가 신규 가입 경로다. /signup만 막고 이 줄을 빠뜨리면 구글 버튼으로
        #    초대 게이트가 통째로 우회된다.
        _require_invite(payload.invite_code)

        # openreview_id는 베타에서 받지 않는다 (이메일 가입과 같은 이유).
        # 예전에는 여기서 400으로 되돌려보내 프론트가 다시 물어보게 했다.
        if not email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="구글 계정에 이메일이 없습니다."
            )
        user = User(
            email=email,
            password_hash=None,
            nickname=(claims.get("name") or email)[:50],
            google_sub=google_sub,
            openreview_id=_openreview_id_or_placeholder(payload.openreview_id),
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


# ------------------------------------------------------------ 비밀번호 재설정
# 이게 없으면 비밀번호를 잊은 사용자는 계정을 영영 쓸 수 없다 (schemas/auth.py의
# _MAX_LEGACY_PASSWORD_LEN 주석이 그 상황을 이미 전제하고 있었다).
#
# ⚠️ **메일 발송이 아직 없다.** 개발 환경에서는 토큰을 로그로 남기고, production
# 에서는 503을 돌려준다 — app/core/mail.py 참고. 흐름과 검증은 여기서 끝나 있으니
# 발송 수단만 붙이면 된다.

_invalid_reset_token = HTTPException(
    status_code=status.HTTP_400_BAD_REQUEST,
    detail="재설정 링크가 유효하지 않거나 만료되었습니다.",
)


@router.post("/password/forgot", response_model=ApiResponse[Message])
@limiter.limit("5/minute")
def forgot_password(request: Request, payload: PasswordForgotRequest,
                    db: Session = Depends(get_db)):
    """재설정 토큰을 발급해 메일로 보낸다.

    **계정이 없어도, 구글 전용 계정이어도 응답은 똑같다.** 응답이 갈리면 이
    엔드포인트가 "이 이메일이 가입돼 있는가"를 알려주는 조회창이 된다.

    구글 전용 계정(password_hash가 NULL)에는 보내지 않는다 — 그 계정에 비밀번호를
    새로 심는 것은 '재설정'이 아니라 '설정'이고, 로그인 수단을 바꾸는 일이라 별도
    판단이 필요하다. 그런 사용자는 구글로 로그인하면 된다.
    """
    user = db.query(User).filter(User.email == payload.email).first()
    if user is not None and user.password_hash is not None:
        token = create_password_reset_token(subject=str(user.user_id),
                                            version=user.token_version)
        try:
            deliver_password_reset(user.email, token)
        except MailNotConfigured as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
            )
    return ApiResponse[Message](
        data=Message(message="가입된 이메일이라면 재설정 링크를 보냈습니다.")
    )


@router.post("/password/reset", response_model=ApiResponse[Message])
@limiter.limit("10/minute")
def reset_password(request: Request, payload: PasswordResetRequest,
                   db: Session = Depends(get_db)):
    """재설정 토큰으로 새 비밀번호를 정한다.

    성공하면 token_version을 올린다. 두 가지를 한 번에 처리한다 —
    **이 재설정 토큰이 재사용되지 않고**(ver이 어긋난다), 유출된 비밀번호로 이미
    발급된 refresh_token도 전부 죽는다. 비밀번호를 되찾는 상황은 계정이 남의 손에
    있었을 가능성이 큰 상황이라, 기존 세션을 남겨두면 안 된다.
    """
    try:
        decoded = decode_token(payload.token)
    except JWTError:
        raise _invalid_reset_token

    if decoded.get("type") != "password_reset":
        raise _invalid_reset_token

    try:
        user = db.get(User, uuid.UUID(decoded.get("sub")))
    except (ValueError, TypeError):
        raise _invalid_reset_token

    if user is None or decoded.get("ver") != user.token_version:
        raise _invalid_reset_token
    if user.password_hash is None:
        # 발급 시점에 걸렀지만, 그 사이 계정이 구글 전용으로 바뀌었을 수 있다.
        raise _invalid_reset_token

    user.password_hash = hash_password(payload.new_password)
    user.token_version += 1
    db.commit()
    return ApiResponse[Message](data=Message(message="비밀번호가 변경되었습니다."))
