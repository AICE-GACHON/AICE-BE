from fastapi import FastAPI

from app.core.error_handlers import register_exception_handlers
from app.routers import auth, user, paper, review, submission, feedback
from app.schemas.common import ApiResponse

app = FastAPI(
    title="AICE API",
    description="논문 평가 및 피드백 서비스 백엔드",
    version="0.1.0",
)

register_exception_handlers(app)

# 도메인별 라우터 등록
app.include_router(auth.router)
app.include_router(user.router)
app.include_router(paper.router)
app.include_router(review.router)
app.include_router(submission.router)
app.include_router(feedback.router)


@app.get("/", response_model=ApiResponse[dict[str, str]])
def health_check():
    """서버가 살아있는지 확인하는 기본 엔드포인트"""
    return ApiResponse[dict[str, str]](data={"service": "AICE-BE", "status": "running"})
