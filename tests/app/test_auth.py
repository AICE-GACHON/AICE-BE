"""회원가입·로그인·토큰 검증."""
from tests.app.conftest import _unique_openreview_id


def test_signup_returns_user_without_password(client):
    openreview_id = _unique_openreview_id()
    res = client.post("/api/auth/signup", json={
        "email": "brand-new@example.com", "password": "password123",
        "nickname": "새사용자", "openreview_id": openreview_id})
    assert res.status_code == 201
    body = res.json()
    assert body["success"] is True
    assert body["data"]["email"] == "brand-new@example.com"
    assert body["data"]["openreview_id"] == openreview_id
    # 이메일로 가입한 계정이므로 구글은 아직 붙어 있지 않다
    assert body["data"]["google_linked"] is False
    assert "password" not in body["data"] and "password_hash" not in body["data"]


def test_signup_rejects_duplicate_email(client, auth):
    res = client.post("/api/auth/signup", json={
        "email": auth["email"], "password": "password123", "nickname": "중복",
        "openreview_id": _unique_openreview_id()})
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "409"


def test_signup_rejects_duplicate_openreview_id(client):
    """openreview_id에 unique 제약을 걸기 전에는 이게 그냥 통과됐다
    (alembic 0006_unique_openreview_id).

    이메일과 달리 라우터에 사전 체크가 없어서 IntegrityError 경로로만 걸린다 —
    제약이 사라지면 여기서 201이 나온다.
    """
    first = client.post("/api/auth/signup", json={
        "email": "orig@example.com", "password": "password123",
        "nickname": "원본", "openreview_id": "~Dup_Target1"})
    assert first.status_code == 201

    res = client.post("/api/auth/signup", json={
        "email": "another@example.com", "password": "password123",
        "nickname": "중복ID", "openreview_id": "~Dup_Target1"})
    assert res.status_code == 409


def test_signup_without_openreview_id_gets_placeholder(client):
    """베타에서는 openreview_id를 받지 않는다 — 서버가 자리표시자를 만든다.

    예전에는 422로 거절했다(필수였다). 지금 이 값을 읽는 기능이 없어서 가입
    문턱만 높이고 있었다. 컬럼은 여전히 unique·not null이므로 **비워둘 수는
    없고**, 계정마다 고유한 값이어야 한다 — 같은 값을 넣으면 두 번째 가입자가
    IntegrityError로 거절된다.
    """
    res = client.post("/api/auth/signup", json={
        "email": "no-id@example.com", "password": "password123",
        "nickname": "아이디없음"})
    assert res.status_code == 201
    assert res.json()["data"]["openreview_id"].startswith("pending:")


def test_signup_placeholders_are_unique_per_account(client):
    """자리표시자가 계정마다 달라야 한다. 같으면 두 번째 가입이 통째로 막힌다."""
    ids = []
    for i in range(2):
        res = client.post("/api/auth/signup", json={
            "email": f"placeholder{i}@example.com", "password": "password123",
            "nickname": f"사용자{i}"})
        assert res.status_code == 201, res.text
        ids.append(res.json()["data"]["openreview_id"])
    assert ids[0] != ids[1]


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


# ------------------------------------------------------- refresh / logout 회전

def test_login_then_refresh_then_logout_invalidates_refresh(client, auth):
    """로그아웃은 users.token_version을 올려 **이미 발급된** refresh_token까지 끊는다.

    access_token은 JWT라 만료 전까지 살아 있지만, 재발급 경로가 막히므로 세션이
    연장되지 않는다. 이 고리가 끊기면 로그아웃이 사실상 무의미해진다.
    """
    tokens = client.post("/api/auth/login", json={
        "email": auth["email"], "password": auth["password"]}).json()["data"]
    assert tokens["access_token"] and tokens["refresh_token"]

    rotated = client.post("/api/auth/refresh",
                          json={"refresh_token": tokens["refresh_token"]})
    assert rotated.status_code == 200
    new_tokens = rotated.json()["data"]

    logout = client.post("/api/auth/logout", headers={
        "Authorization": f"Bearer {new_tokens['access_token']}"})
    assert logout.status_code == 200

    # 로그아웃 직전에 회전받은 refresh_token조차 이제는 ver이 어긋나 무효다
    stale = client.post("/api/auth/refresh",
                        json={"refresh_token": new_tokens["refresh_token"]})
    assert stale.status_code == 401
