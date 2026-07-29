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


def test_submission_ownership_returns_404_for_other_user(cleanup_users):
    owner = _signup_payload()
    assert _signup(owner).status_code == 201
    cleanup_users.append(owner["email"])
    owner_tokens = _login(owner["email"], owner["password"]).json()["data"]

    other = _signup_payload()
    assert _signup(other).status_code == 201
    cleanup_users.append(other["email"])
    other_tokens = _login(other["email"], other["password"]).json()["data"]

    create_resp = client.post(
        "/api/submissions",
        json={"title": "Test Paper", "abstract": "abstract text"},
        headers={"Authorization": f"Bearer {owner_tokens['access_token']}"},
    )
    assert create_resp.status_code == 201, create_resp.text
    submission_id = create_resp.json()["data"]["submission_id"]

    other_headers = {"Authorization": f"Bearer {other_tokens['access_token']}"}
    assert client.get(f"/api/submissions/{submission_id}", headers=other_headers).status_code == 404
    assert client.delete(f"/api/submissions/{submission_id}", headers=other_headers).status_code == 404

    owner_headers = {"Authorization": f"Bearer {owner_tokens['access_token']}"}
    assert client.get(f"/api/submissions/{submission_id}", headers=owner_headers).status_code == 200
