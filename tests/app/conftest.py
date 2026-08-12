"""백엔드 테스트 공통 준비.

실제 Postgres를 쓴다 — 모델이 UUID/JSONB 같은 postgres 전용 타입과 부분 유니크
인덱스를 쓰기 때문에 SQLite로 바꿔치기하면 정작 검증하고 싶은 것(중복 분석 방지 등)이
빠진다. 대신 **테스트마다 트랜잭션을 통째로 롤백**해서 개발 DB에 흔적을 남기지 않는다.

컨테이너가 없거나 마이그레이션이 안 돼 있으면 전체를 skip한다.

    docker compose up -d && alembic upgrade head && pytest
"""
import pathlib
import uuid

import pytest
from sqlalchemy import inspect, text

from app.core import mail
from app.core.rate_limit import limiter
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

_HERE = pathlib.Path(__file__).parent


def pytest_collection_modifyitems(items):
    """DB가 준비되지 않았으면 이 디렉터리의 테스트를 전부 skip으로 표시한다.

    **conftest.py에 `pytestmark`를 두는 것으로는 아무 일도 일어나지 않는다.**
    pytestmark는 테스트 모듈과 클래스에서만 적용되고 conftest에서는 조용히 무시된다.
    예전에 여기 `pytestmark = pytest.mark.skipif(not _READY, ...)`가 있었지만,
    로컬에는 DB가 늘 떠 있어(_READY=True) 아무도 그게 죽은 코드인 줄 몰랐다.
    CI의 smoke 잡(DB 없이 도는)에서 처음으로 skip 대신 psycopg.OperationalError가
    쏟아지며 드러났다. 훅으로 바꾸면 DB 유무와 무관하게 동작이 같아진다.

    훅은 수집된 **전체** 항목을 받으므로(루트 아래 tests/paper_assistant, tests/meta
    포함) 이 디렉터리 밑의 것만 골라야 한다.
    """
    if _READY:
        return
    skip = pytest.mark.skip(reason=_REASON)
    for item in items:
        if _HERE in pathlib.Path(str(item.path)).parents:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _disable_rate_limit():
    """인증 없는 엔드포인트(signup/login 10/분 등)를 테스트가 반복 호출하면 429가
    난다. 테스트 동안만 끈다 (app/core/rate_limit.py)."""
    limiter.enabled = False
    yield
    limiter.enabled = True


@pytest.fixture(autouse=True)
def _no_ambient_smtp(monkeypatch):
    """테스트는 **주변 환경의 SMTP 설정을 절대 쓰지 않는다.**

    `.env`에 진짜 SES 자격증명을 넣는 순간(배포를 만지면 실제로 그렇게 된다)
    테스트 전체가 두 가지로 망가진다:

    1. **진짜 SES로 요청이 나간다.** 재설정 흐름을 다루는 테스트마다
       `user-xxxx@example.com` 앞으로 발송을 시도한다. 미검증 주소라 SES가
       거절하지만 **요청은 실제로 나가고 일일 한도를 깎는다.** 수신자가 하필
       검증된 주소라면 테스트가 남에게 메일을 보낸다.
    2. **테스트가 실패한다.** `test_password_reset.py`의 `reset_token` 픽스처는
       "미설정이면 링크를 로그로 남긴다"는 개발용 경로에 의존해 토큰을 얻는데
       (app/core/mail.py), 설정이 채워져 있으면 그 경로를 안 타서 토큰을 못 얻는다.

    그래서 기본값을 **미설정으로 고정**한다. 설정된 상태가 필요한 테스트는
    직접 채워 쓴다(tests/app/test_mail.py의 `smtp_configured`). autouse가 먼저
    돌고 명시 픽스처가 뒤에 덮으므로 순서 문제는 없다.
    """
    for name in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM"):
        monkeypatch.setattr(mail.settings, name, "")


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


def _unique_openreview_id() -> str:
    """openreview_id는 필수이자 unique다 (0006_unique_openreview_id).
    테스트끼리 겹치면 409가 나므로 매번 새로 만든다."""
    return f"~Test_User{uuid.uuid4().hex[:12]}1"


@pytest.fixture
def signup(client):
    """회원가입 + 로그인까지 마친 뒤 토큰을 돌려주는 헬퍼."""
    def _signup(password: str = "password123") -> dict:
        email = _unique_email()
        res = client.post("/api/auth/signup", json={
            "email": email, "password": password, "nickname": "tester",
            "openreview_id": _unique_openreview_id()})
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


# ------------------------------------------------------------------ PDF 헬퍼

def make_pdf(title: str = "Graph Neural Networks for Molecules",
             abstract: str = "We propose a message-passing GNN for molecules.",
             pages: int = 1) -> bytes:
    """추출기가 실제로 읽어낼 수 있는 최소 논문 PDF를 만든다.

    바이트를 하드코딩하지 않고 매번 조립하는 이유는 **페이지 수를 파라미터로 받아야**
    하기 때문이다 — 페이지 상한(60p) 검사에는 진짜 다중 페이지 PDF가 필요하다.

    조판은 pdf/extract.py가 기대하는 형태를 맞춘다: 제목이 첫 페이지에서 가장 큰
    폰트(17pt), 저자는 작게(10pt, 폰트 크기로 걸러진다), 초록은 'Abstract'와
    'Introduction' 사이. 초록은 **한 줄에 들어가는 길이로 유지할 것** — 직접 줄을
    나누면 단어가 쪼개져 검증이 지저분해진다.
    """
    import fitz  # PyMuPDF

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), title, fontsize=17)
    page.insert_text((72, 130), "Jane Doe, John Roe", fontsize=10)
    page.insert_text((72, 170), "Abstract", fontsize=12)
    page.insert_text((72, 190), abstract, fontsize=10)
    page.insert_text((72, 230), "1 Introduction", fontsize=12)
    for _ in range(pages - 1):
        doc.new_page().insert_text((72, 100), "body text", fontsize=10)
    try:
        return doc.tobytes()
    finally:
        doc.close()


def upload_pdf(client, auth, *, title: str | None = "Graph Neural Networks for Molecules",
               abstract: str | None = "We propose a message-passing GNN for molecules.",
               field: str | None = "ML", pages: int = 1, pdf: bytes | None = None,
               filename: str = "paper.pdf"):
    """POST /api/submissions/pdf 원본 응답. 실패 사례도 봐야 하므로 검증하지 않는다.

    title/abstract를 None으로 주면 폼에서 빼서 **서버가 PDF에서 추출하게** 한다.
    """
    data = {}
    if title is not None:
        data["title"] = title
    if abstract is not None:
        data["abstract"] = abstract
    if field is not None:
        data["field"] = field
    body = pdf if pdf is not None else make_pdf(pages=pages)
    return client.post(
        "/api/submissions/pdf", data=data,
        files={"pdf": (filename, body, "application/pdf")},
        headers=auth["headers"])


# Base는 여기서 쓰지 않지만, 모델이 등록된 상태인지 확인하는 안전장치.
assert SERVICE_TABLES <= set(Base.metadata.tables)
