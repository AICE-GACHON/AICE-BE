"""백엔드 테스트 공통 준비.

실제 Postgres를 쓴다 — 모델이 UUID/JSONB 같은 postgres 전용 타입과 부분 유니크
인덱스를 쓰기 때문에 SQLite로 바꿔치기하면 정작 검증하고 싶은 것(중복 분석 방지 등)이
빠진다. 대신 **테스트마다 트랜잭션을 통째로 롤백**해서 개발 DB에 흔적을 남기지 않는다.

컨테이너가 없거나 마이그레이션이 안 돼 있으면 전체를 skip한다.

    docker compose up -d && alembic upgrade head && pytest
"""
import uuid

import pytest
from sqlalchemy import inspect, text

from app.database import Base, SessionLocal, engine, get_db
from app.main import app
from app.routers import submissions as submissions_router

TestClient = pytest.importorskip("fastapi.testclient").TestClient

SERVICE_TABLES = {"users", "submissions", "review_predictions",
                  "similar_paper_matches"}


def _schema_ready() -> tuple[bool, str]:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            existing = set(inspect(conn).get_table_names())
    except Exception as exc:
        return False, f"Postgres 미기동 (docker compose up -d): {type(exc).__name__}"
    missing = SERVICE_TABLES - existing
    if missing:
        return False, f"서비스 테이블 없음 {sorted(missing)} — alembic upgrade head 먼저"
    return True, ""


_READY, _REASON = _schema_ready()
pytestmark = pytest.mark.skipif(not _READY, reason=_REASON)


@pytest.fixture
def db():
    """바깥 트랜잭션 안에서 도는 세션. 테스트가 끝나면 전부 롤백된다.

    라우터가 db.commit()을 부르지만 join_transaction_mode="create_savepoint"라
    savepoint만 커밋되고, 바깥 트랜잭션을 롤백하면 함께 사라진다.
    """
    connection = engine.connect()
    transaction = connection.begin()
    session = SessionLocal(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def client(db, monkeypatch):
    """인증 없는 클라이언트. 분석 백그라운드 작업은 실행되지 않게 막아둔다.

    TestClient는 BackgroundTasks를 응답 직후 동기로 실행한다. 막지 않으면
    테스트가 SPECTER2를 로드하고 진짜 분석을 돌린다.
    """
    started: list[uuid.UUID] = []
    monkeypatch.setattr(submissions_router, "run_analysis", started.append)

    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        c.started_analyses = started
        yield c
    app.dependency_overrides.clear()


def _unique_email() -> str:
    return f"user-{uuid.uuid4().hex[:12]}@example.com"


@pytest.fixture
def signup(client):
    """회원가입 + 로그인까지 마친 뒤 토큰을 돌려주는 헬퍼."""
    def _signup(password: str = "password123") -> dict:
        email = _unique_email()
        res = client.post("/api/auth/signup", json={
            "email": email, "password": password, "nickname": "tester"})
        assert res.status_code == 201, res.text
        token = client.post("/api/auth/login", json={
            "email": email, "password": password}).json()["data"]["access_token"]
        return {"email": email, "password": password, "token": token,
                "headers": {"Authorization": f"Bearer {token}"}}
    return _signup


@pytest.fixture
def auth(signup):
    return signup()


@pytest.fixture
def other_user(signup):
    """남의 계정 — 소유권 검증용."""
    return signup()


# Base는 여기서 쓰지 않지만, 모델이 등록된 상태인지 확인하는 안전장치.
assert SERVICE_TABLES <= set(Base.metadata.tables)
