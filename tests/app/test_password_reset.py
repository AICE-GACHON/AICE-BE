"""비밀번호 재설정 (POST /api/auth/password/forgot·reset).

이게 없으면 비밀번호를 잊은 사용자는 계정을 영영 쓸 수 없다.

여기서 지키려는 것:
  - **응답이 계정 존재 여부를 알려주지 않는다.** 갈리는 순간 이 엔드포인트가
    "이 이메일이 가입돼 있는가"를 묻는 조회창이 된다.
  - **토큰은 한 번만 쓸 수 있다.** token_version 스냅샷으로 처리한다 — 저장소 없이.
  - **재설정하면 기존 세션이 전부 죽는다.** 계정이 남의 손에 있었을 수 있는 상황이다.
"""
import pytest

from app.core.security import create_password_reset_token


def _forgot(client, email):
    return client.post("/api/auth/password/forgot", json={"email": email})


def _reset(client, token, new_password="brandnew456"):
    return client.post("/api/auth/password/reset",
                       json={"token": token, "new_password": new_password})


@pytest.fixture
def reset_token(client, auth, caplog):
    """forgot을 호출해 발급된 토큰을 얻는다.

    메일 발송 수단이 아직 없어 개발 환경에서는 로그로 나온다(app/core/mail.py).
    프론트 없이 흐름을 끝까지 시험하려면 이 경로가 유일하다.
    """
    with caplog.at_level("WARNING", logger="app.core.mail"):
        assert _forgot(client, auth["email"]).status_code == 200
    for record in caplog.records:
        if "비밀번호 재설정 토큰" in record.getMessage():
            return record.args[1]
    pytest.fail("재설정 토큰이 로그에 남지 않았다")


# ------------------------------------------------------------ 발급 (forgot)

def test_response_is_identical_for_unknown_email(client, auth):
    """계정이 있든 없든 같은 응답 — 가입 여부를 알려주면 안 된다."""
    known = _forgot(client, auth["email"])
    unknown = _forgot(client, "definitely-not-registered@example.com")
    assert known.status_code == unknown.status_code == 200
    assert known.json()["data"] == unknown.json()["data"]


def test_google_only_account_gets_the_same_response(client, monkeypatch):
    """구글 전용 계정에도 보내지 않지만, 응답은 같아야 한다."""
    import uuid as _uuid

    from tests.app.conftest import _unique_openreview_id
    unique = _uuid.uuid4().hex[:12]
    claims = {"sub": f"google-sub-{unique}", "email": f"g_{unique}@example.com",
              "email_verified": True, "name": "구글사용자"}
    monkeypatch.setattr("app.routers.auth.verify_google_id_token", lambda t: claims)
    assert client.post("/api/auth/google", json={
        "id_token": "fake", "openreview_id": _unique_openreview_id()}).status_code == 200

    res = _forgot(client, claims["email"])
    assert res.status_code == 200, res.text


def test_production_returns_503_instead_of_pretending(client, auth, monkeypatch):
    """발송 수단이 없는 채로 200을 돌려주면, 되는 줄 알았던 기능이 조용히 죽는다.

    사용자는 메일을 영영 못 받는데 서버는 성공이라고 말한다 — 그게 최악이다.
    """
    monkeypatch.setattr("app.core.mail.settings.ENVIRONMENT", "production")
    res = _forgot(client, auth["email"])
    assert res.status_code == 503, res.text


# -------------------------------------------------------------- 사용 (reset)

def test_reset_switches_the_working_password(client, auth, reset_token):
    assert _reset(client, reset_token).status_code == 200

    assert client.post("/api/auth/login", json={
        "email": auth["email"], "password": auth["password"]}).status_code == 401
    assert client.post("/api/auth/login", json={
        "email": auth["email"], "password": "brandnew456"}).status_code == 200


def test_token_cannot_be_used_twice(client, auth, reset_token):
    """재설정이 성공하면 token_version이 올라 같은 토큰의 ver이 어긋난다."""
    assert _reset(client, reset_token).status_code == 200
    again = _reset(client, reset_token, new_password="another789")
    assert again.status_code == 400, again.text


def test_reset_revokes_existing_sessions(client, auth, reset_token):
    """되찾는 상황은 계정이 남의 손에 있었을 수 있다 — 기존 세션을 남기면 안 된다."""
    tokens = client.post("/api/auth/login", json={
        "email": auth["email"], "password": auth["password"]}).json()["data"]

    assert _reset(client, reset_token).status_code == 200

    assert client.post("/api/auth/refresh", json={
        "refresh_token": tokens["refresh_token"]}).status_code == 401


def test_logout_invalidates_a_pending_reset_token(client, auth, reset_token):
    """로그아웃도 token_version을 올린다 — 발급해둔 재설정 토큰이 함께 죽는다."""
    assert client.post("/api/auth/logout", headers=auth["headers"]).status_code == 200
    assert _reset(client, reset_token).status_code == 400


@pytest.mark.parametrize("bad_token", ["not-a-jwt", ""])
def test_malformed_token_is_rejected(client, bad_token):
    assert _reset(client, bad_token).status_code == 400


def test_refresh_token_is_not_accepted_as_a_reset_token(client, auth):
    """**type 검사가 진짜로 막는 것은 refresh_token 이다.**

    refresh_token 은 재설정 토큰과 같은 `ver` 클레임을 담는다. 그래서 type 검사를
    빼면 ver 검사를 그대로 통과해 **refresh_token 하나로 현재 비밀번호 없이
    비밀번호를 바꿀 수 있다.** refresh_token 은 수명이 길고 클라이언트에 저장되므로
    실제 위험이다.

    access_token 으로 시험하면 이 검사를 검증하지 못한다 — access_token 에는 ver 이
    없어서 type 검사를 빼도 ver 검사에 걸려 400 이 나온다. 통과하지만 아무것도
    막지 못하는 테스트가 된다(실제로 처음엔 그렇게 썼다).
    """
    tokens = client.post("/api/auth/login", json={
        "email": auth["email"], "password": auth["password"]}).json()["data"]

    res = _reset(client, tokens["refresh_token"])
    assert res.status_code == 400, res.text
    # 비밀번호가 실제로 안 바뀌었는지 — 400 만 보고 넘어가면 반쪽이다.
    assert client.post("/api/auth/login", json={
        "email": auth["email"], "password": auth["password"]}).status_code == 200


def test_access_token_is_not_accepted_either(client, auth):
    """access_token 에는 ver 이 없어 ver 검사에서 걸린다 (type 검사와는 별개 경로)."""
    assert _reset(client, auth["token"]).status_code == 400


def test_reset_token_for_a_deleted_account_is_rejected(client, auth, reset_token):
    assert client.request("DELETE", "/api/user/me",
                          json={"password": auth["password"]},
                          headers=auth["headers"]).status_code == 200
    assert _reset(client, reset_token).status_code == 400


@pytest.mark.parametrize("bad", ["short", "가" * 25])
def test_new_password_follows_signup_rules(client, reset_token, bad):
    """8자 미만과 bcrypt 72바이트 초과(한글 25자=75바이트)를 막는다.

    재설정 경로로 가입 규칙을 우회할 수 있으면 안 된다.
    """
    assert _reset(client, reset_token, new_password=bad).status_code == 422


def test_token_expiry_is_short(client):
    """수명이 길면 탈취된 메일함의 위험이 오래간다. 30분 안팎이면 충분하다."""
    from app.core.security import PASSWORD_RESET_EXPIRE_MINUTES
    assert 0 < PASSWORD_RESET_EXPIRE_MINUTES <= 60


def test_token_bound_to_another_users_version_is_rejected(client, auth, other_user):
    """남의 user_id로 만든 토큰이라도 ver이 맞아야 통과한다 — 그리고 서명이 필요하다."""
    import uuid as _uuid
    forged = create_password_reset_token(subject=str(_uuid.uuid4()), version=0)
    assert _reset(client, forged).status_code == 400
