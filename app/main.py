import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.core.errors import register_exception_handlers
from app.routers import auth, corpus, submissions, user
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


app = FastAPI(
    title="AICE API",
    description="논문 평가 및 피드백 서비스 백엔드",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

register_exception_handlers(app)

# 도메인별 라우터 등록
app.include_router(auth.router)
app.include_router(user.router)
app.include_router(submissions.router)
app.include_router(corpus.router)


@app.get("/", response_model=ApiResponse[dict[str, str]])
def health_check():
    """서버가 살아있는지 확인하는 기본 엔드포인트"""
    return ApiResponse[dict[str, str]](data={"service": "AICE-BE", "status": "running"})
