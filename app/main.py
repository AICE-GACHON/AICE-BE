import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi.middleware import SlowAPIMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.core.config import settings
from app.core.errors import register_exception_handlers
from app.core.middleware import BodySizeLimitMiddleware, SecurityHeadersMiddleware
from app.core.rate_limit import limiter
from app.routers import auth, corpus, onboarding, submissions, user
from app.schemas.common import ApiResponse

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """WARMUP_ON_STARTUP=1이면 첫 분석 요청의 지연(SPECTER2 로드 수십 초)을
    기동 시점으로 옮긴다.

    기본은 off다 — 켜면 매 기동이 그만큼 느려져서 로컬 개발과 테스트가 답답해진다.
    배포 환경에서만 켜는 것을 권한다. 실패해도 서버는 정상 기동한다.
    """
    if not settings.WARMUP_ON_STARTUP:
        yield
        return
    try:
        from paper_assistant.graph.pipeline import warmup

        log.info("분석 파이프라인 워밍업 (SPECTER2 로드)...")
        warmup()
        log.info("워밍업 완료")
    except Exception as exc:
        log.warning("워밍업 실패 — 첫 분석 요청 때 로드됩니다: %s", exc)
    yield


_is_production = settings.ENVIRONMENT == "production"

app = FastAPI(
    title="AICE API",
    description="논문 평가 및 피드백 서비스 백엔드",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.ENABLE_DOCS else None,
    redoc_url="/redoc" if settings.ENABLE_DOCS else None,
    openapi_url="/openapi.json" if settings.ENABLE_DOCS else None,
)

app.state.limiter = limiter

# 미들웨어는 **등록의 역순으로** 요청을 만난다. 본문 상한을 마지막에 등록해
# 가장 먼저 돌게 하는 이유는, 24MB짜리 요청이 CORS·rate limit·라우팅을 거치기
# 전에 끊기는 편이 싸기 때문이다.

app.add_middleware(SlowAPIMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    # 실제로 쓰는 메서드/헤더만 연다. "*"는 allow_credentials=True와 함께 쓰면
    # 브라우저가 무시하기도 하고, 무엇보다 나중에 늘어난 헤더가 검토 없이
    # 자동으로 허용된다.
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
    max_age=600,
)

# https 뒤에서 서비스할 때만 HSTS를 켠다 (개발 중 http localhost를 브라우저가
# https로 기억해버리는 사고를 피한다).
app.add_middleware(
    SecurityHeadersMiddleware,
    enable_hsts=_is_production,
    enable_docs=settings.ENABLE_DOCS,
)

# Host 헤더를 그대로 믿지 않는다. 기본값 ["*"]는 개발용이고, production에서
# "*"를 남기면 app.core.config의 검증이 기동을 막는다.
app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.ALLOWED_HOSTS)

app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.MAX_REQUEST_BYTES)

register_exception_handlers(app)

# 도메인별 라우터 등록
app.include_router(auth.router)
app.include_router(user.router)
app.include_router(submissions.router)
app.include_router(corpus.router)
app.include_router(onboarding.router)


@app.get("/", response_model=ApiResponse[dict[str, str]])
def health_check():
    """서버가 살아있는지 확인하는 기본 엔드포인트"""
    return ApiResponse[dict[str, str]](data={"service": "AICE-BE", "status": "running"})
