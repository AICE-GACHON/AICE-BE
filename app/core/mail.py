"""사용자에게 보내는 메일.

**아직 발송 수단이 없다.** SMTP도 SES도 붙어 있지 않고 자격증명도 없다. 그래서
지금은 개발 환경에서 링크를 로그로 남기는 것까지만 한다.

production 에서 조용히 "로그에만 남기는" 동작을 하면 최악이다 — 서버는 200을
돌려주고 사용자는 메일을 영영 못 받는데, 아무도 그걸 모른다. 그래서 production
에서는 예외를 던져 **기능이 꺼져 있다는 사실이 503으로 드러나게** 한다.

메일 발송을 붙일 때 고칠 곳은 이 파일 하나다 (`deliver_password_reset` 본문).
"""
import logging

from app.core.config import settings

log = logging.getLogger(__name__)


class MailNotConfigured(RuntimeError):
    """보낼 수단이 없어서 전달하지 못했다. 라우터가 503으로 바꾼다."""


def deliver_password_reset(email: str, reset_token: str) -> None:
    """비밀번호 재설정 토큰을 사용자에게 전달한다.

    개발 환경에서는 로그로 남긴다 — 프론트 없이도 흐름을 끝까지 시험할 수 있어야
    한다. 토큰이 로그에 남는 것은 개발 환경에서만 허용되는 타협이다.
    """
    if settings.ENVIRONMENT == "production":
        raise MailNotConfigured(
            "메일 발송이 연결되지 않아 비밀번호 재설정을 보낼 수 없습니다."
        )
    log.warning(
        "[개발용] 비밀번호 재설정 토큰 — %s : %s\n"
        "  (메일 발송이 붙기 전까지 이 로그로 확인한다. app/core/mail.py)",
        email, reset_token,
    )
