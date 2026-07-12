from fastapi import APIRouter

# 도메인: auth
# API 명세서(Notion)의 'auth' 도메인 API들을 여기에 하나씩 구현합니다.
router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.get("/ping")
def ping():
    """
    auth 라우터가 정상 연결됐는지 확인하는 임시 테스트 엔드포인트.
    실제 API 구현하면서 이 함수는 지우면 됩니다.
    """
    return {"domain": "auth", "status": "ok"}
