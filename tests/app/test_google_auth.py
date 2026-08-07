"""구글 로그인 — 계정 매칭/생성 분기 (POST /api/auth/google).

진짜 id_token을 만들려면 구글의 개인키가 필요하므로, 검증 함수를 갈아끼워
"구글이 이런 클레임을 돌려줬다"는 상황만 만든다. 여기서 확인하는 것은 토큰 검증
자체가 아니라 **그 뒤의 분기**다: google_sub → email → 신규가입 순서로 계정을
찾고, 못 찾으면 openreview_id를 요구하고, email_verified를 믿지 않는다.
"""
import uuid

import pytest

from tests.app.conftest import _unique_openreview_id


def _fake_claims(**overrides) -> dict:
    unique = uuid.uuid4().hex[:12]
    claims = {
        "sub": f"google-sub-{unique}",
        "email": f"gtest_{unique}@example.com",
        "email_verified": True,
        "name": f"구글사용자_{unique}",
    }
    claims.update(overrides)
    return claims


@pytest.fixture
def google_claims(monkeypatch):
    """POST /api/auth/google이 보게 될 클레임을 테스트가 정해준다.

    라우터가 `from app.core.google_oauth import verify_google_id_token`으로 이름을
    당겨왔으므로, 원본 모듈이 아니라 라우터 네임스페이스 쪽을 갈아끼워야 한다.
    """
    box = {}

    def _fake_verify(token: str) -> dict:
        return box["claims"]

    monkeypatch.setattr("app.routers.auth.verify_google_id_token", _fake_verify)

    def _set(claims: dict) -> dict:
        box["claims"] = claims
        return claims

    return _set


def _google_login(client, **payload):
    return client.post("/api/auth/google",
                       json={"id_token": "fake-id-token", **payload})


def test_verify_google_id_token_rejects_malformed_token():
    """검증 함수 자체. 형식이 깨진 토큰은 구글 인증서를 받아오기도 전에 걸러진다
    (google-auth가 던지는 ValueError/GoogleAuthError를 ValueError로 합쳐놨다)."""
    from app.core.google_oauth import verify_google_id_token

    with pytest.raises(ValueError):
        verify_google_id_token("not-a-jwt")


def test_google_login_invalid_token_returns_401(client, monkeypatch):
    def _reject(token: str) -> dict:
        raise ValueError("invalid token")

    monkeypatch.setattr("app.routers.auth.verify_google_id_token", _reject)
    assert _google_login(client).status_code == 401


def test_google_login_new_user_requires_openreview_id(client, google_claims):
    google_claims(_fake_claims())
    res = _google_login(client)
    assert res.status_code == 400
    # 프론트(AICE-FE src/api/auth.js loginWithGoogle)가 이 메시지에 'openreview_id'가
    # 들어 있는지로 "ID를 입력받아 같은 토큰으로 재시도" 분기를 탄다 — 문구를 바꾸면
    # 프론트의 최초 가입 플로우가 조용히 깨진다.
    assert "openreview_id" in res.json()["error"]["message"]


def test_google_login_new_user_creates_linked_account(client, google_claims):
    claims = google_claims(_fake_claims())

    res = _google_login(client, openreview_id=_unique_openreview_id())
    assert res.status_code == 200, res.text
    tokens = res.json()["data"]

    me = client.get("/api/user/me",
                    headers={"Authorization": f"Bearer {tokens['access_token']}"})
    assert me.status_code == 200, me.text
    body = me.json()["data"]
    assert body["email"] == claims["email"]
    assert body["google_linked"] is True


def test_google_login_unverified_email_returns_401(client, google_claims):
    """email_verified가 false면 그 이메일이 그 구글 계정 소유라는 보장이 없다 —
    이걸 통과시키면 남의 email/password 계정을 가로챌 수 있다."""
    google_claims(_fake_claims(email_verified=False))
    res = _google_login(client, openreview_id=_unique_openreview_id())
    assert res.status_code == 401


def test_google_login_links_existing_email_account(client, auth, google_claims):
    """이메일로 가입한 계정에 같은 이메일의 구글 계정이 연동되고, 연동 후에도
    비밀번호 로그인이 계속 살아 있어야 한다 (password_hash를 지우지 않는다)."""
    google_claims(_fake_claims(email=auth["email"]))

    res = _google_login(client)  # 기존 계정에 연동되는 경우라 openreview_id는 필요 없다
    assert res.status_code == 200, res.text

    tokens = res.json()["data"]
    me = client.get("/api/user/me",
                    headers={"Authorization": f"Bearer {tokens['access_token']}"})
    assert me.json()["data"]["google_linked"] is True
    assert me.json()["data"]["email"] == auth["email"]

    login = client.post("/api/auth/login", json={
        "email": auth["email"], "password": auth["password"]})
    assert login.status_code == 200
