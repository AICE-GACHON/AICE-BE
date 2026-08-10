"""가입 초대 게이트 (SIGNUP_INVITE_CODE).

이 게이트가 지키는 것은 계정이 아니라 **예산**이다. 돈이 나가는 경로(분석,
/story?refresh=true)가 전부 로그인 뒤에 있으므로, 가입이 잠기면 지출이 초대
인원 안에 묶인다 (docs/배포_계획.md D5).

**여기서 제일 중요한 테스트는 구글 쪽이다.** 가입 경로가 둘인데 /signup만 막으면
구글 버튼으로 통째로 우회된다 — google_login()이 처음 보는 계정을 그 자리에서
새로 만들기 때문이다. 그 구멍을 테스트로 못박는다.
"""
import uuid

import pytest

from app.core.config import settings
from tests.app.conftest import _unique_email, _unique_openreview_id

CODE = "test-invite-code-1234"


@pytest.fixture
def invite_required(monkeypatch):
    """SIGNUP_INVITE_CODE가 설정된 배포 상태를 만든다.

    settings는 기동 시 한 번 만들어지는 인스턴스라, 라우터가 읽는 그 객체의
    속성을 갈아끼운다 (라우터는 `settings.SIGNUP_INVITE_CODE`를 호출 시점에
    읽으므로 이걸로 충분하다).
    """
    monkeypatch.setattr(settings, "SIGNUP_INVITE_CODE", CODE)
    return CODE


def _signup_payload(**overrides) -> dict:
    payload = {
        "email": _unique_email(),
        "password": "password123",
        "nickname": "tester",
        "openreview_id": _unique_openreview_id(),
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------- 이메일 가입

def test_signup_open_when_no_code_configured(client):
    """기본값(빈 문자열)에서는 아무것도 달라지지 않는다 — 로컬 개발이 막히면 안 된다."""
    res = client.post("/api/auth/signup", json=_signup_payload())
    assert res.status_code == 201, res.text


def test_signup_rejected_without_code(client, invite_required):
    res = client.post("/api/auth/signup", json=_signup_payload())
    assert res.status_code == 403, res.text


def test_signup_rejected_with_wrong_code(client, invite_required):
    res = client.post("/api/auth/signup", json=_signup_payload(invite_code="nope"))
    assert res.status_code == 403, res.text


def test_signup_accepted_with_correct_code(client, invite_required):
    res = client.post("/api/auth/signup", json=_signup_payload(invite_code=CODE))
    assert res.status_code == 201, res.text


def test_wrong_code_does_not_leak_whether_email_exists(client, invite_required):
    """초대받지 않은 사람은 **이메일 존재 여부를 알 수 없어야** 한다.

    검사를 DB 조회 뒤에 두면 이미 가입된 이메일은 409, 아닌 이메일은 403이 나와서
    초대 코드 없이도 가입자 명단을 훑을 수 있다. 그래서 게이트가 DB보다 먼저 온다.
    """
    taken = _signup_payload(invite_code=CODE)
    assert client.post("/api/auth/signup", json=taken).status_code == 201

    existing = client.post("/api/auth/signup", json=_signup_payload(
        email=taken["email"], invite_code="wrong"))
    fresh = client.post("/api/auth/signup", json=_signup_payload(invite_code="wrong"))

    assert existing.status_code == fresh.status_code == 403
    assert existing.json() == fresh.json()


# ----------------------------------------------------------------- 구글 가입

def _google_login(client, **payload):
    return client.post("/api/auth/google", json={"id_token": "fake-id-token", **payload})


def _claims(**overrides) -> dict:
    unique = uuid.uuid4().hex[:12]
    return {"sub": f"google-sub-{unique}", "email": f"g_{unique}@example.com",
            "email_verified": True, "name": "구글사용자", **overrides}


@pytest.fixture
def google_claims(monkeypatch):
    box = {}
    monkeypatch.setattr("app.routers.auth.verify_google_id_token",
                        lambda token: box["claims"])

    def _set(claims: dict) -> dict:
        box["claims"] = claims
        return claims

    return _set


def test_google_signup_is_gated_too(client, invite_required, google_claims):
    """🔴 이 테스트가 이 파일의 존재 이유다.

    /signup만 막고 구글 경로를 빠뜨리면, 초대 게이트가 있다고 믿는 상태로
    누구나 구글 버튼을 눌러 계정을 만들고 분석(1회 약 $0.30)을 돌릴 수 있다.
    """
    google_claims(_claims())
    res = _google_login(client, openreview_id=_unique_openreview_id())
    assert res.status_code == 403, res.text


def test_google_signup_accepted_with_correct_code(client, invite_required, google_claims):
    google_claims(_claims())
    res = _google_login(client, openreview_id=_unique_openreview_id(), invite_code=CODE)
    assert res.status_code == 200, res.text


def test_existing_google_user_logs_in_without_code(client, invite_required, google_claims):
    """이미 초대받아 가입한 사람은 **로그인할 때마다 코드를 다시 낼 필요가 없다.**

    매번 요구하면 그건 초대가 아니라 두 번째 비밀번호다. 게이트는 신규 가입
    분기에만 걸려 있어야 한다.
    """
    claims = google_claims(_claims())
    first = _google_login(client, openreview_id=_unique_openreview_id(), invite_code=CODE)
    assert first.status_code == 200, first.text

    google_claims(claims)          # 같은 구글 계정으로 다시 로그인
    again = _google_login(client)  # 코드 없이
    assert again.status_code == 200, again.text


def test_google_linking_existing_email_account_needs_no_code(client, invite_required,
                                                             google_claims):
    """이메일로 이미 가입한 계정에 구글을 연동하는 것도 신규 가입이 아니다."""
    payload = _signup_payload(invite_code=CODE)
    assert client.post("/api/auth/signup", json=payload).status_code == 201

    google_claims(_claims(email=payload["email"]))
    res = _google_login(client)
    assert res.status_code == 200, res.text
