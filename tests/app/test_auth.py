"""회원가입·로그인·토큰 검증."""
from tests.app.conftest import _unique_openreview_id


def test_signup_returns_user_without_password(client):
    res = client.post("/api/auth/signup", json={
        "email": "brand-new@example.com", "password": "password123",
        "nickname": "새사용자", "openreview_id": _unique_openreview_id()})
    assert res.status_code == 201
    body = res.json()
    assert body["success"] is True
    assert body["data"]["email"] == "brand-new@example.com"
    assert "password" not in body["data"] and "password_hash" not in body["data"]


def test_signup_rejects_duplicate_email(client, auth):
    res = client.post("/api/auth/signup", json={
        "email": auth["email"], "password": "password123", "nickname": "중복",
        "openreview_id": _unique_openreview_id()})
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "409"


def test_signup_rejects_short_password(client):
    res = client.post("/api/auth/signup", json={
        "email": "short@example.com", "password": "1234", "nickname": "짧음",
        "openreview_id": _unique_openreview_id()})
    assert res.status_code == 422
    # 검증 실패도 공통 포맷으로 나와야 한다
    assert res.json()["success"] is False


def test_login_rejects_wrong_password(client, auth):
    res = client.post("/api/auth/login", json={
        "email": auth["email"], "password": "wrong-password"})
    assert res.status_code == 401


def test_login_rejects_unknown_email(client):
    res = client.post("/api/auth/login", json={
        "email": "nobody@example.com", "password": "password123"})
    # 계정 존재 여부가 드러나면 안 되므로 오답 비밀번호와 같은 응답이어야 한다
    assert res.status_code == 401


def test_me_requires_token(client):
    assert client.get("/api/user/me").status_code == 401


def test_me_rejects_garbage_token(client):
    res = client.get("/api/user/me",
                     headers={"Authorization": "Bearer not-a-real-token"})
    assert res.status_code == 401


def test_me_returns_current_user(client, auth):
    res = client.get("/api/user/me", headers=auth["headers"])
    assert res.status_code == 200
    assert res.json()["data"]["email"] == auth["email"]
