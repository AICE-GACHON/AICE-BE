from fastapi import FastAPI

from app.routers import auth, card, performance, merchant, recommendation, user

app = FastAPI(
    title="AICE API",
    description="위치 기반 카드 혜택 추천 서비스 백엔드",
    version="0.1.0",
)

# 도메인별 라우터 등록
# 주의: 각 라우터의 prefix는 임시로 도메인 이름을 그대로 썼습니다.
# 실제 API 명세서(Notion)를 보면 URI가 도메인마다 조금씩 다르게 잡혀 있으니
# (예: performance 도메인의 실제 URI는 /api/users/me/performances/... 또는 /api/codef/...),
# 각 엔드포인트를 실제로 구현할 때 데코레이터의 경로를 API 명세서에 맞게 수정하세요.
app.include_router(auth.router)
app.include_router(card.router)
app.include_router(performance.router)
app.include_router(merchant.router)
app.include_router(recommendation.router)
app.include_router(user.router)


@app.get("/")
def health_check():
    """서버가 살아있는지 확인하는 기본 엔드포인트"""
    return {"service": "AICE-BE", "status": "running"}
