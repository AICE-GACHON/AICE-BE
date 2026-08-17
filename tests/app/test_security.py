"""배포 대비 보안 방어의 회귀 테스트.

여기 있는 것들은 전부 **없어도 기능은 멀쩡히 도는** 방어라, 리팩터링 중에 조용히
사라지기 쉽다. 사라졌다는 사실이 사고가 나기 전에는 드러나지 않으므로 테스트로 못박는다.

⚠️ conftest의 `_disable_rate_limit` fixture가 전역으로 rate limit을 끈다. 상한을
검증하는 테스트는 `live_limiter`로 다시 켠 뒤, **자기 카운터를 직접 비운다** —
저장소가 프로세스 메모리라 테스트끼리 카운트가 새어 나가기 때문이다.
"""
import uuid

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.rate_limit import limiter, user_or_ip
from app.core.security import create_access_token

STRONG_SECRET = "x" * 40


@pytest.fixture
def live_limiter():
    """이 테스트 동안만 rate limit을 실제로 켠다 (카운터도 비운 상태로)."""
    limiter.reset()
    limiter.enabled = True
    yield
    limiter.enabled = False
    limiter.reset()


# ------------------------------------------------------------------ 응답 헤더

def test_security_headers_are_on_every_response(client):
    """브라우저가 이 응답으로 할 수 있는 일을 최소로 줄이는 헤더들."""
    res = client.get("/")
    assert res.headers["X-Content-Type-Options"] == "nosniff"
    assert res.headers["X-Frame-Options"] == "DENY"
    assert res.headers["Referrer-Policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in res.headers["Content-Security-Policy"]


def test_hsts_is_off_in_development(client):
    """http로 개발하는 중에 HSTS를 켜면 브라우저가 localhost를 https로 기억해
    버려서, 그 뒤로 개발 서버에 접속이 안 되는 상태가 캐시 수명만큼 이어진다."""
    assert "Strict-Transport-Security" not in client.get("/").headers


# ------------------------------------------------------------------ 본문 상한

def test_oversized_body_is_rejected_before_it_is_read(client):
    """Content-Length가 상한을 넘으면 본문을 읽지 않고 413으로 끊는다."""
    res = client.post("/api/onboarding", json={"user_type": "student"},
                      headers={"Content-Length": str(100 * 1024 * 1024)})
    assert res.status_code == 413
    assert res.json()["success"] is False


def test_oversized_pdf_is_rejected_without_buffering_it(client, auth):
    """상한 검사가 읽기보다 **먼저** 와야 한다.

    예전에는 `await pdf.read()`로 통째로 읽은 뒤 길이를 봤다 — 거부할 파일도 일단
    전부 메모리에 올라가서, 로그인한 사용자 한 명이 서버를 넘어뜨릴 수 있었다.
    """
    big = b"%PDF-1.4\n" + b"0" * (21 * 1024 * 1024)
    res = client.post("/api/submissions/pdf", data={"title": "t", "abstract": "a"},
                      files={"pdf": ("big.pdf", big, "application/pdf")},
                      headers=auth["headers"])
    assert res.status_code == 413


# ---------------------------------------------------------- 인증 없는 입력 상한

@pytest.mark.parametrize("payload", [
    {"user_type": "x" * 500},              # String(50) 컬럼 — 예전엔 DB에서 터져 500
    {"venue": ["x" * 500]},                # JSONB — 원소 길이 제한이 없었다
    {"fields": ["y" * 5000]},              # JSONB — 원소 길이 제한이 없었다
    {"fields": ["a"] * 500},               # JSONB — 개수 제한이 없었다
])
def test_onboarding_rejects_oversized_input_with_422(client, payload):
    """온보딩은 인증이 없다. 상한이 없으면 누구나 DB를 부풀릴 수 있고,
    컬럼 길이를 넘긴 값은 500(서버 잘못)으로 나갔다 — 422여야 한다."""
    assert client.post("/api/onboarding", json=payload).status_code == 422


def test_password_longer_than_bcrypt_limit_is_rejected(client):
    """bcrypt는 72바이트를 넘는 입력을 조용히 잘라낸다. 거부하지 않으면 사용자가
    긴 비밀번호를 쓰고도 앞 72바이트만 보호받으면서 그 사실을 모른다."""
    res = client.post("/api/auth/signup", json={
        "agreed_to_terms": True,
        "email": "long@example.com", "password": "z" * 200,
        "nickname": "n", "openreview_id": f"~T{uuid.uuid4().hex[:8]}1"})
    assert res.status_code == 422


def test_huge_token_never_reaches_the_verifier(client):
    assert client.post("/api/auth/refresh",
                       json={"refresh_token": "t" * 50_000}).status_code == 422


# ------------------------------------------------------------------ 정보 노출

def test_failed_analysis_does_not_leak_exception_text(client, auth, db, monkeypatch):
    """분석 실패 사유는 GET .../analysis의 error로 **사용자에게 그대로 나간다.**

    예외 문자열을 그대로 저장하면 psycopg의 DB 호스트·사용자명, anthropic의 요청
    URL, 파일 경로가 함께 나간다. 원인은 로그에만 남아야 한다.
    """
    from app.models.analysis import ReviewPrediction
    from app.services import analysis as svc
    from tests.app.conftest import upload_pdf

    secret = "postgresql://paper:hunter2@10.0.0.7:5433/paper_assistant"
    submission_id = upload_pdf(client, auth).json()["data"]["submission_id"]
    prediction = ReviewPrediction(submission_id=uuid.UUID(submission_id), status="pending")
    db.add(prediction)
    db.commit()

    def boom(*a, **kw):
        raise RuntimeError(f"connection to {secret} failed")

    monkeypatch.setattr("paper_assistant.analyze", boom, raising=False)
    monkeypatch.setattr(svc, "SessionLocal", lambda: db)
    svc.run_analysis(prediction.prediction_id)

    res = client.get(f"/api/submissions/{submission_id}/analysis", headers=auth["headers"])
    error = res.json()["data"]["error"] or ""
    assert res.json()["data"]["status"] == "failed"
    assert "hunter2" not in error and "10.0.0.7" not in error and "RuntimeError" not in error


def test_login_runs_bcrypt_even_for_unknown_accounts(monkeypatch, client):
    """계정이 없을 때만 bcrypt를 건너뛰면 응답 시간이 곧 '이 이메일이 가입돼
    있는가'의 답이 된다 — 메시지를 아무리 똑같이 맞춰도 소용없다."""
    calls: list[str] = []
    import app.routers.auth as auth_router
    monkeypatch.setattr(auth_router, "dummy_verify", lambda pw: calls.append(pw))

    res = client.post("/api/auth/login",
                      json={"email": "nobody@example.com", "password": "whatever12"})
    assert res.status_code == 401
    assert calls == ["whatever12"], "없는 계정에도 비밀번호 검증 비용을 치러야 한다"


# ------------------------------------------------------------------ rate limit

def test_cost_endpoints_are_limited_per_account_not_per_ip(client, auth, live_limiter):
    """PDF 업로드는 호출마다 LLM 비용이 든다. 인증만으로는 예산을 지킬 수 없다 —
    계정 하나만 만들면 무한히 부를 수 있기 때문이다."""
    from tests.app.conftest import upload_pdf

    seen = {upload_pdf(client, auth).status_code for _ in range(22)}
    assert 429 in seen, "계정당 시간당 20회를 넘겨도 막히지 않았다"


def test_rate_limit_key_is_the_account_when_logged_in():
    """키가 IP면 한 사람이 IP를 바꿔가며 예산을 비울 수 있고, 반대로 NAT 뒤의
    여러 사용자가 서로의 몫을 잡아먹는다."""
    user_id = str(uuid.uuid4())
    token = create_access_token(subject=user_id)
    request = _fake_request({"authorization": f"Bearer {token}"})
    assert user_or_ip(request) == f"user:{user_id}"


@pytest.mark.parametrize("headers", [
    {},                                        # 비로그인
    {"authorization": "Bearer forged.token.x"},  # 위조 — 서명 검증에서 걸린다
])
def test_rate_limit_falls_back_to_ip_without_a_valid_token(headers):
    """위조 토큰으로 임의의 sub를 넣어 상한을 우회할 수 없어야 한다."""
    assert user_or_ip(_fake_request(headers)).startswith("ip:")


def _fake_request(headers: dict):
    from starlette.requests import Request

    return Request({
        "type": "http", "method": "GET", "path": "/", "http_version": "1.1",
        "client": ("203.0.113.9", 1234), "server": ("test", 80), "scheme": "http",
        "query_string": b"", "root_path": "",
        "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
    })


# ------------------------------------------------------- 배포 설정 안전장치

def test_placeholder_jwt_secret_never_boots():
    """이 문자열은 공개 저장소(.env.example)에 적혀 있다. 그대로 배포하면 누구나
    임의 user_id로 access_token을 위조해 아무 계정으로나 로그인할 수 있다."""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, JWT_SECRET_KEY="change-this-to-a-random-secret-key")


def test_short_jwt_secret_never_boots():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, JWT_SECRET_KEY="tooshort")


@pytest.mark.parametrize("overrides, why", [
    ({}, "localhost CORS가 남아 있다"),
    ({"CORS_ORIGINS": ["*"], "ALLOWED_HOSTS": ["a.com"]}, "CORS 와일드카드"),
    ({"CORS_ORIGINS": ["http://a.com"], "ALLOWED_HOSTS": ["a.com"]}, "평문 http origin"),
    ({"CORS_ORIGINS": ["https://a.com"]}, "ALLOWED_HOSTS가 '*'"),
])
def test_production_refuses_to_boot_with_dev_defaults(overrides, why):
    """경고 로그로 두지 않는 이유: 기동 로그는 아무도 읽지 않는다."""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, JWT_SECRET_KEY=STRONG_SECRET,
                 ENVIRONMENT="production", **overrides)


@pytest.fixture
def use_llm(monkeypatch):
    """USE_LLM은 Settings 필드가 아니라 paper_assistant.config를 읽는 property다
    (공유 값의 단일 소스). 그래서 생성 인자가 아니라 그 모듈을 갈아끼운다."""
    def _set(value: bool) -> None:
        monkeypatch.setattr("paper_assistant.config.USE_LLM", value)

    return _set


def _prod(**overrides) -> dict:
    """production 설정 검증용 기본값.

    **SMTP를 명시적으로 비운다.** `_env_file=None`만으로는 주변 환경이 새어 들어와,
    개발자 `.env`에 진짜 SES 자격증명이 들어 있으면(배포를 만지면 실제로 그렇게 된다)
    SMTP 반쪽 설정 가드에 걸려 **이 파일의 테스트들이 엉뚱한 이유로 실패한다.**
    여기서 보려는 것은 초대 게이트·docs 노출이지 메일이 아니므로, 메일은 꺼진
    상태로 고정한다. 메일 가드 자체는 test_mail.py가 본다.

    **FRONTEND_BASE_URL도 채운다.** 기본값이 localhost인데 production에서는
    메일 설정과 무관하게 거부된다(공유 링크가 이 값으로 만들어지기 때문). 그
    가드 자체는 아래 test_production_refuses_dev_frontend_url이 본다.
    """
    base = dict(_env_file=None, JWT_SECRET_KEY=STRONG_SECRET, ENVIRONMENT="production",
                CORS_ORIGINS=["https://a.com"], ALLOWED_HOSTS=["a.com"],
                FRONTEND_BASE_URL="https://a.com",
                SMTP_HOST="", SMTP_USER="", SMTP_PASSWORD="", SMTP_FROM="")
    return {**base, **overrides}


def test_production_refuses_dev_frontend_url():
    """**메일을 켜지 않았어도** 거부한다.

    예전에는 이 검사가 SMTP 블록 안에만 있었다. 그때는 이 값을 쓰는 곳이 재설정
    메일뿐이었기 때문인데, 지금은 공유 링크(app/services/shares.py)도 같은 값으로
    주소를 만든다 — 메일 없이 배포하면 사용자가 발급한 공유 링크가 전부
    `http://localhost:5173/shared/...`로 나가고, 받는 사람은 자기 컴퓨터를 연다.
    실패가 **남의 브라우저에서** 일어나 우리 로그에는 아무것도 남지 않는다.
    """
    for bad in ("http://localhost:5173", "http://127.0.0.1:3000", "http://a.com"):
        with pytest.raises(ValidationError):
            Settings(**_prod(FRONTEND_BASE_URL=bad))

    assert Settings(**_prod(FRONTEND_BASE_URL="https://a.com")).FRONTEND_BASE_URL


def test_production_refuses_llm_with_open_signup(use_llm):
    """LLM을 켠 채 가입이 열려 있으면 **아무나 계정을 만들어 우리 돈을 쓴다.**

    분석 1회가 약 $0.30이고 청구는 Anthropic에서 우리 카드로 온다. 다른 가드들이
    '틀리면 공개되는' 종류라면 이건 '틀리면 청구되는' 종류다.
    """
    use_llm(True)
    with pytest.raises(ValidationError):
        Settings(**_prod(SIGNUP_INVITE_CODE=""))


def test_production_allows_llm_when_signup_is_gated(use_llm):
    use_llm(True)
    assert Settings(**_prod(SIGNUP_INVITE_CODE="some-code")).SIGNUP_INVITE_CODE


def test_production_allows_open_signup_when_llm_is_off(use_llm):
    """조합만 막는다. LLM이 꺼져 있으면(스텁, $0) 가입을 열어둔 채 화면 흐름만
    보여주는 것은 정상적인 배포 형태다."""
    use_llm(False)
    assert Settings(**_prod(SIGNUP_INVITE_CODE="")).ENVIRONMENT == "production"


def test_production_closes_the_api_docs_unless_asked():
    """OpenAPI 스키마는 전체 엔드포인트·파라미터를 그대로 알려주는 공격 지도다."""
    prod = _prod()
    assert Settings(**prod).ENABLE_DOCS is False
    # 열려면 명시해야 한다 — 그 선택이 .env에 기록으로 남게.
    assert Settings(**prod, ENABLE_DOCS=True).ENABLE_DOCS is True
