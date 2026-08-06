"""app/routers/* 백엔드 라우터 통합 테스트 (paper_assistant 테스트와 별개).

실제 서비스 테이블(users, submissions, ...)에 대고 돈다. Postgres가 떠 있고
alembic upgrade head가 적용돼 있어야 한다:

    docker compose up -d
    alembic upgrade head
    pytest tests/test_backend_auth.py

DB가 없으면 모듈 전체가 skip된다 (app.core.config.Settings는 DATABASE_URL이
필수라, DB 없이 무작정 import하면 pytest 수집 자체가 깨지므로 아래에서 연결
가능 여부를 먼저 확인하고, 안 되면 app을 import하기 전에 skip한다).

⚠️ 이 파일은 로컬에 Python 실행 환경이 없는 상태에서 작성됐다 — 코드 리뷰로만
검증했고 실제로 돌려본 적은 없다. 처음 실행할 때 실패하면 먼저 이 파일을 의심할 것.
"""
import os
import uuid

import pytest
from dotenv import load_dotenv

load_dotenv()
_DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://paper:paper@localhost:5433/paper_assistant"
)


def _db_available() -> bool:
    try:
        import psycopg

        with psycopg.connect(_DATABASE_URL, connect_timeout=3):
            return True
    except Exception:
        return False


if not _db_available():
    pytest.skip(
        "Postgres 미기동 또는 DATABASE_URL 미설정 "
        "(docker compose up -d && alembic upgrade head 필요)",
        allow_module_level=True,
    )

from fastapi.testclient import TestClient  # noqa: E402

from app.core.rate_limit import limiter  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models.user import User  # noqa: E402

client = TestClient(app)


@pytest.fixture(autouse=True)
def _disable_rate_limit():
    """이 파일이 signup/login 등을 반복 호출해도 10/min 같은 제한에 걸리지
    않도록 테스트 동안만 rate limit을 끈다 (app/core/rate_limit.py)."""
    limiter.enabled = False
    yield
    limiter.enabled = True


@pytest.fixture
def cleanup_users():
    """테스트가 만든 유저를 이메일 기준으로 뒤처리한다.

    submissions/review_predictions 등은 users FK가 ON DELETE CASCADE라
    User를 지우면 같이 정리된다 (alembic 0001).
    """
    created_emails: list[str] = []
    yield created_emails
    if created_emails:
        db = SessionLocal()
        try:
            db.query(User).filter(User.email.in_(created_emails)).delete(synchronize_session=False)
            db.commit()
        finally:
            db.close()


def _signup_payload(**overrides) -> dict:
    unique = uuid.uuid4().hex[:12]
    payload = {
        "email": f"test_{unique}@example.com",
        "password": "testpassword123",
        "nickname": f"tester_{unique}",
        "openreview_id": f"~Test_User_{unique}",
    }
    payload.update(overrides)
    return payload


def _signup(payload: dict):
    return client.post("/api/auth/signup", json=payload)


def _login(email: str, password: str):
    return client.post("/api/auth/login", json={"email": email, "password": password})


def test_signup_requires_openreview_id(cleanup_users):
    payload = _signup_payload()
    del payload["openreview_id"]
    resp = _signup(payload)
    assert resp.status_code == 422


def test_signup_success_returns_user(cleanup_users):
    payload = _signup_payload()
    resp = _signup(payload)
    assert resp.status_code == 201, resp.text
    cleanup_users.append(payload["email"])

    body = resp.json()
    assert body["success"] is True
    assert body["data"]["email"] == payload["email"]
    assert body["data"]["openreview_id"] == payload["openreview_id"]
    assert body["data"]["google_linked"] is False


def test_signup_duplicate_email_returns_409(cleanup_users):
    payload = _signup_payload()
    assert _signup(payload).status_code == 201
    cleanup_users.append(payload["email"])

    dup = _signup_payload(email=payload["email"])
    resp = _signup(dup)
    assert resp.status_code == 409


def test_signup_duplicate_openreview_id_returns_409(cleanup_users):
    """리뷰에서 발견한 이슈: openreview_id에 unique 제약을 걸기 전에는 이게 그냥
    통과됐다 (alembic 0005_unique_openreview_id.py)."""
    payload = _signup_payload()
    assert _signup(payload).status_code == 201
    cleanup_users.append(payload["email"])

    dup = _signup_payload(openreview_id=payload["openreview_id"])
    resp = _signup(dup)
    assert resp.status_code == 409
    cleanup_users.append(dup["email"])  # 409면 실제로 안 만들어지지만 혹시 몰라 등록


def test_login_wrong_password_returns_401(cleanup_users):
    payload = _signup_payload()
    assert _signup(payload).status_code == 201
    cleanup_users.append(payload["email"])

    resp = _login(payload["email"], "wrong-password")
    assert resp.status_code == 401


def test_login_then_refresh_then_logout_invalidates_refresh(cleanup_users):
    payload = _signup_payload()
    assert _signup(payload).status_code == 201
    cleanup_users.append(payload["email"])

    login_resp = _login(payload["email"], payload["password"])
    assert login_resp.status_code == 200
    tokens = login_resp.json()["data"]
    assert tokens["access_token"] and tokens["refresh_token"]

    refresh_resp = client.post("/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert refresh_resp.status_code == 200
    new_tokens = refresh_resp.json()["data"]

    logout_resp = client.post(
        "/api/auth/logout", headers={"Authorization": f"Bearer {new_tokens['access_token']}"}
    )
    assert logout_resp.status_code == 200

    # 로그아웃 이전에 발급된 refresh_token은 이제 무효화됐어야 한다
    # (users.token_version이 올라가서 예전 refresh_token의 ver과 어긋남).
    stale_resp = client.post("/api/auth/refresh", json={"refresh_token": new_tokens["refresh_token"]})
    assert stale_resp.status_code == 401


# --- 구글 로그인 -------------------------------------------------------------
# 진짜 id_token을 만들려면 구글의 개인키가 필요하므로, 검증 함수를 갈아끼워
# "구글이 이런 클레임을 돌려줬다"는 상황만 만든다. 여기서 확인하는 건 토큰 검증
# 자체가 아니라 그 뒤의 계정 매칭/생성 분기(app/routers/auth.py google_login)다.


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


def _google_login(**payload):
    return client.post("/api/auth/google", json={"id_token": "fake-id-token", **payload})


def test_verify_google_id_token_rejects_malformed_token():
    """검증 함수 자체. 형식이 깨진 토큰은 구글 인증서를 받아오기도 전에 걸러진다
    (google-auth가 던지는 ValueError/GoogleAuthError를 ValueError로 합쳐놨다)."""
    from app.core.google_oauth import verify_google_id_token

    with pytest.raises(ValueError):
        verify_google_id_token("not-a-jwt")


def test_google_login_invalid_token_returns_401(monkeypatch):
    def _reject(token: str) -> dict:
        raise ValueError("invalid token")

    monkeypatch.setattr("app.routers.auth.verify_google_id_token", _reject)
    assert _google_login().status_code == 401


def test_google_login_new_user_requires_openreview_id(google_claims):
    google_claims(_fake_claims())
    resp = _google_login()
    assert resp.status_code == 400
    # 프론트(AICE-FE src/api/auth.js loginWithGoogle)가 이 메시지에 'openreview_id'가
    # 들어 있는지로 "ID를 입력받아 같은 토큰으로 재시도" 분기를 탄다 — 문구를 바꾸면
    # 프론트의 최초 가입 플로우가 조용히 깨진다.
    assert "openreview_id" in resp.json()["error"]["message"]


def test_google_login_new_user_creates_linked_account(cleanup_users, google_claims):
    claims = google_claims(_fake_claims())
    cleanup_users.append(claims["email"])

    resp = _google_login(openreview_id=f"~Google_User_{uuid.uuid4().hex[:10]}")
    assert resp.status_code == 200, resp.text
    tokens = resp.json()["data"]

    me = client.get("/api/user/me", headers={"Authorization": f"Bearer {tokens['access_token']}"})
    assert me.status_code == 200, me.text
    body = me.json()["data"]
    assert body["email"] == claims["email"]
    assert body["google_linked"] is True


def test_google_login_unverified_email_returns_401(google_claims):
    """email_verified가 false면 그 이메일이 그 구글 계정 소유라는 보장이 없다 —
    이걸 통과시키면 남의 email/password 계정을 가로챌 수 있다."""
    google_claims(_fake_claims(email_verified=False))
    resp = _google_login(openreview_id=f"~Attacker_{uuid.uuid4().hex[:10]}")
    assert resp.status_code == 401


def test_google_login_links_existing_email_account(cleanup_users, google_claims):
    """이메일로 가입한 계정에 같은 이메일의 구글 계정이 연동되고, 연동 후에도
    비밀번호 로그인이 계속 살아 있어야 한다 (password_hash를 지우지 않는다)."""
    payload = _signup_payload()
    assert _signup(payload).status_code == 201
    cleanup_users.append(payload["email"])

    google_claims(_fake_claims(email=payload["email"]))
    resp = _google_login()  # 기존 계정에 연동되는 경우라 openreview_id는 필요 없다
    assert resp.status_code == 200, resp.text

    tokens = resp.json()["data"]
    me = client.get("/api/user/me", headers={"Authorization": f"Bearer {tokens['access_token']}"})
    assert me.json()["data"]["google_linked"] is True
    assert me.json()["data"]["openreview_id"] == payload["openreview_id"]

    assert _login(payload["email"], payload["password"]).status_code == 200


def test_submission_ownership_returns_404_for_other_user(cleanup_users):
    owner = _signup_payload()
    assert _signup(owner).status_code == 201
    cleanup_users.append(owner["email"])
    owner_tokens = _login(owner["email"], owner["password"]).json()["data"]

    other = _signup_payload()
    assert _signup(other).status_code == 201
    cleanup_users.append(other["email"])
    other_tokens = _login(other["email"], other["password"]).json()["data"]

    from tests.app.conftest import make_pdf

    create_resp = client.post(
        "/api/submissions/pdf",
        data={"title": "Test Paper", "abstract": "abstract text"},
        files={"pdf": ("paper.pdf", make_pdf(), "application/pdf")},
        headers={"Authorization": f"Bearer {owner_tokens['access_token']}"},
    )
    assert create_resp.status_code == 201, create_resp.text
    submission_id = create_resp.json()["data"]["submission_id"]

    other_headers = {"Authorization": f"Bearer {other_tokens['access_token']}"}
    assert client.get(f"/api/submissions/{submission_id}", headers=other_headers).status_code == 404
    assert client.delete(f"/api/submissions/{submission_id}", headers=other_headers).status_code == 404

    owner_headers = {"Authorization": f"Bearer {owner_tokens['access_token']}"}
    assert client.get(f"/api/submissions/{submission_id}", headers=owner_headers).status_code == 200
