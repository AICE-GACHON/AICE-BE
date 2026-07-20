from fastapi import APIRouter

# 도메인: user (내 정보 조회/수정)
router = APIRouter(prefix="/api/user", tags=["user"])


@router.get("/ping")
def ping():
    """
    user 라우터가 정상 연결됐는지 확인하는 임시 테스트 엔드포인트.
    실제 API 구현하면서 이 함수는 지우면 됩니다.
    """
    return {"domain": "user", "status": "ok"}
