"""사용자에게 보내는 메일.

SMTP 설정(`SMTP_HOST` + `SMTP_FROM`)이 채워지면 실제로 보내고, 비어 있으면
개발 환경에서는 링크를 로그로 남기고 **production 에서는 503** 이 된다.
즉 자격증명만 `.env` 에 넣으면 코드 변경 없이 켜진다.

**왜 boto3 가 아니라 SMTP 인가**: AWS SES 는 API 와 SMTP 를 모두 제공한다. SMTP 는
표준 라이브러리라 의존성이 늘지 않고, 공급자를 바꿔도(SES → 지메일 → Resend)
설정값만 바뀐다. boto3 를 쓰면 SES 에 묶이고 배포 이미지가 50MB 남짓 늘어난다.

**발송 실패를 다루는 방식이 두 갈래인 이유**는 아래 deliver_password_reset 참고.
"""
import logging
import smtplib
from email.message import EmailMessage
from urllib.parse import quote

from app.core.config import settings

log = logging.getLogger(__name__)

# SMTP 응답이 없을 때 요청이 통째로 묶이는 것을 막는다. 발송은 요청 스레드에서
# 동기로 일어나므로 상한이 없으면 사용자가 하염없이 기다린다.
SMTP_TIMEOUT_SECONDS = 10

_SUBJECT = "[AICE] 비밀번호 재설정"
_BODY = """\
안녕하세요.

아래 링크에서 새 비밀번호를 설정하실 수 있습니다. 링크는 30분 뒤 만료되며
한 번만 사용할 수 있습니다.

{url}

본인이 요청하지 않으셨다면 이 메일은 무시하셔도 됩니다.
비밀번호는 그대로 유지됩니다.
"""


class MailNotConfigured(RuntimeError):
    """보낼 수단 자체가 없다. 라우터가 503으로 바꾼다."""


def is_configured() -> bool:
    """실제 발송이 가능한 상태인가. 둘 다 있어야 의미가 있다."""
    return bool(settings.SMTP_HOST and settings.SMTP_FROM)


def build_reset_url(reset_token: str) -> str:
    """메일에 넣을 재설정 링크.

    서버는 자기 주소만 알지 **프론트가 어디 있는지 모른다.** 그래서 앞부분을
    설정값에서 받는다. 토큰은 JWT라 이미 URL 안전하지만, 규약이 바뀌어도 깨지지
    않도록 인코딩해 둔다.
    """
    base = settings.FRONTEND_BASE_URL.rstrip("/")
    return f"{base}/reset-password?token={quote(reset_token, safe='')}"


def _send(to: str, url: str) -> None:
    message = EmailMessage()
    message["Subject"] = _SUBJECT
    message["From"] = settings.SMTP_FROM
    message["To"] = to
    message.set_content(_BODY.format(url=url))

    # 465는 처음부터 TLS로 감싸는 포트고, 587은 평문으로 열고 STARTTLS로 올린다.
    # 포트를 보고 갈라야 한다 — 465에 STARTTLS를 걸면 핸드셰이크가 엇갈려 멈춘다.
    if settings.SMTP_PORT == 465:
        smtp_cls, use_starttls = smtplib.SMTP_SSL, False
    else:
        smtp_cls, use_starttls = smtplib.SMTP, True

    with smtp_cls(settings.SMTP_HOST, settings.SMTP_PORT,
                  timeout=SMTP_TIMEOUT_SECONDS) as server:
        if use_starttls:
            server.starttls()
        if settings.SMTP_USER:
            server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        server.send_message(message)


def deliver_password_reset(email: str, reset_token: str) -> None:
    """비밀번호 재설정 링크를 사용자에게 전달한다.

    **실패를 두 갈래로 나눈다 — 계정 존재 여부가 새지 않게 하려는 것이다.**

    1. **설정 자체가 없음** → `MailNotConfigured` (라우터가 503).
       모든 요청에 똑같이 일어나므로 누출이 없고, 발송이 안 되는데 200을 돌려주면
       사용자는 메일을 영영 못 받는데 서버는 성공이라고 말하게 된다.
    2. **설정은 있는데 발송 실패** → 로그만 남기고 조용히 넘어간다.
       여기서 503을 돌려주면 **"실패했다 = 그 계정이 존재한다"** 가 되어, 가입
       여부를 숨기려고 만든 규약이 깨진다. 없는 계정은 애초에 발송을 시도조차
       하지 않아 항상 빠르게 200이기 때문이다.
    """
    url = build_reset_url(reset_token)

    if not is_configured():
        if settings.ENVIRONMENT == "production":
            raise MailNotConfigured(
                "메일 발송이 설정되지 않아 비밀번호 재설정을 보낼 수 없습니다."
            )
        log.warning(
            "[개발용] 메일 발송 미설정 — 재설정 링크를 로그로 남긴다\n"
            "  받는 사람: %s\n  링크: %s\n"
            "  (.env에 SMTP_HOST/SMTP_FROM을 넣으면 실제로 발송된다)",
            email, url,
        )
        return

    try:
        _send(email, url)
    except (smtplib.SMTPException, OSError):
        # 계정 존재 여부를 숨기기 위해 삼킨다(위 2번). 대신 스택까지 남겨
        # 로그에서는 반드시 보이게 한다.
        log.exception("비밀번호 재설정 메일 발송 실패 — 받는 사람 %s", email)
