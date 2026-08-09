"""메일 발송 (app/core/mail.py).

자격증명이 없어도 **여기까지는 전부 검증할 수 있다** — 설정을 넣는 순간 켜지는지,
안 넣으면 어떻게 되는지, 메일 내용과 링크가 맞는지. 실제 SMTP 서버로 나가는
한 걸음만 남는다.

특히 **실패를 두 갈래로 나눈 이유**를 고정한다:
  - 설정 자체가 없음 → 예외(503). 모두에게 같으므로 누출이 없다.
  - 설정은 있는데 발송 실패 → 삼킨다. 503을 주면 "실패 = 그 계정 있음"이 되어
    가입 여부를 숨기려던 규약이 깨진다.
"""
import smtplib

import pytest

from app.core import mail


@pytest.fixture
def smtp_configured(monkeypatch):
    """SMTP 설정이 채워진 상태를 만든다 (자격증명 없이)."""
    monkeypatch.setattr(mail.settings, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(mail.settings, "SMTP_PORT", 587)
    monkeypatch.setattr(mail.settings, "SMTP_USER", "user")
    monkeypatch.setattr(mail.settings, "SMTP_PASSWORD", "pw")
    monkeypatch.setattr(mail.settings, "SMTP_FROM", "noreply@example.com")


@pytest.fixture
def sent(monkeypatch):
    """실제로 나가는 대신 잡아둔다. (host, port, starttls, message)"""
    box = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            box["host"], box["port"], box["timeout"] = host, port, timeout
            box["starttls"] = False

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self):
            box["starttls"] = True

        def login(self, user, password):
            box["login"] = (user, password)

        def send_message(self, message):
            box["message"] = message

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    return box


# ------------------------------------------------------------------ 링크

def test_reset_url_points_at_the_frontend(monkeypatch):
    """서버는 자기 주소만 안다 — 링크 앞부분은 설정에서 와야 한다."""
    monkeypatch.setattr(mail.settings, "FRONTEND_BASE_URL", "https://aice.example.com")
    url = mail.build_reset_url("abc.def.ghi")
    assert url == "https://aice.example.com/reset-password?token=abc.def.ghi"


def test_reset_url_tolerates_a_trailing_slash(monkeypatch):
    monkeypatch.setattr(mail.settings, "FRONTEND_BASE_URL", "https://aice.example.com/")
    assert "//reset-password" not in mail.build_reset_url("t")


def test_reset_url_escapes_the_token(monkeypatch):
    """지금 토큰은 JWT라 안전하지만, 규약이 바뀌어도 링크가 깨지지 않아야 한다."""
    monkeypatch.setattr(mail.settings, "FRONTEND_BASE_URL", "https://x.example.com")
    assert "a%2Fb" in mail.build_reset_url("a/b")


# --------------------------------------------------------------- 설정 판정

def test_needs_both_host_and_from(monkeypatch):
    monkeypatch.setattr(mail.settings, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(mail.settings, "SMTP_FROM", "")
    assert mail.is_configured() is False

    monkeypatch.setattr(mail.settings, "SMTP_FROM", "noreply@example.com")
    assert mail.is_configured() is True


# ------------------------------------------------------- 설정이 없을 때

def test_development_logs_the_link(monkeypatch, caplog):
    monkeypatch.setattr(mail.settings, "SMTP_HOST", "")
    monkeypatch.setattr(mail.settings, "ENVIRONMENT", "development")
    with caplog.at_level("WARNING", logger="app.core.mail"):
        mail.deliver_password_reset("u@example.com", "tok123")
    assert "tok123" in caplog.text


def test_production_refuses_instead_of_pretending(monkeypatch):
    """발송이 안 되는데 성공이라고 말하면 사용자는 메일을 영영 못 받는다."""
    monkeypatch.setattr(mail.settings, "SMTP_HOST", "")
    monkeypatch.setattr(mail.settings, "ENVIRONMENT", "production")
    with pytest.raises(mail.MailNotConfigured):
        mail.deliver_password_reset("u@example.com", "tok123")


# ------------------------------------------------------- 설정이 있을 때

def test_sends_with_the_configured_sender_and_link(
        monkeypatch, smtp_configured, sent):
    monkeypatch.setattr(mail.settings, "FRONTEND_BASE_URL", "https://aice.example.com")
    mail.deliver_password_reset("user@example.com", "tok999")

    msg = sent["message"]
    assert msg["From"] == "noreply@example.com"
    assert msg["To"] == "user@example.com"
    body = msg.get_content()
    assert "https://aice.example.com/reset-password?token=tok999" in body
    assert sent["login"] == ("user", "pw")
    assert sent["timeout"] == mail.SMTP_TIMEOUT_SECONDS


def test_port_587_uses_starttls(monkeypatch, smtp_configured, sent):
    monkeypatch.setattr(mail.settings, "SMTP_PORT", 587)
    mail.deliver_password_reset("u@example.com", "t")
    assert sent["starttls"] is True


def test_port_465_does_not_call_starttls(monkeypatch, smtp_configured, sent):
    """465는 처음부터 TLS로 감싼 포트다. STARTTLS를 걸면 핸드셰이크가 엇갈린다."""
    monkeypatch.setattr(mail.settings, "SMTP_PORT", 465)
    mail.deliver_password_reset("u@example.com", "t")
    assert sent["starttls"] is False


def test_send_failure_is_swallowed_to_hide_account_existence(
        monkeypatch, smtp_configured, caplog):
    """여기서 예외를 올리면 **"실패 = 그 계정이 있다"** 가 되어 버린다.

    없는 계정은 발송을 시도조차 하지 않아 항상 빠르게 200이므로, 발송 실패만
    503이 되면 응답 차이로 가입 여부를 알아낼 수 있다.
    """
    def _boom(*a, **kw):
        raise smtplib.SMTPException("서버가 거절함")

    monkeypatch.setattr(mail, "_send", _boom)
    with caplog.at_level("ERROR", logger="app.core.mail"):
        mail.deliver_password_reset("u@example.com", "t")   # 예외가 나오면 안 된다
    assert "발송 실패" in caplog.text


def test_network_error_is_swallowed_too(monkeypatch, smtp_configured, caplog):
    """SMTPException 만 잡으면 연결 거부·타임아웃(OSError)이 500으로 샌다."""
    def _boom(*a, **kw):
        raise OSError("connection refused")

    monkeypatch.setattr(mail, "_send", _boom)
    with caplog.at_level("ERROR", logger="app.core.mail"):
        mail.deliver_password_reset("u@example.com", "t")
    assert "발송 실패" in caplog.text
