from fastapi import FastAPI

from app.routers import auth, user, paper, review, submission, feedback

app = FastAPI(
    title="AICE API",
    description="논문 평가 및 피드백 서비스 백엔드",
    version="0.1.0",
)

# 도메인별 라우터 등록
# 각 라우터는 아직 /ping 테스트 엔드포인트만 있는 뼈대 상태입니다.
# 실제 API 명세서를 확정한 뒤, 각 라우터 파일 안에 진짜 엔드포인트를 추가하세요.
app.include_router(auth.router)
app.include_router(user.router)
app.include_router(paper.router)
app.include_router(review.router)
app.include_router(submission.router)
app.include_router(feedback.router)


@app.get("/")
def health_check():
    """서버가 살아있는지 확인하는 기본 엔드포인트"""
    return {"service": "AICE-BE", "status": "running"}
