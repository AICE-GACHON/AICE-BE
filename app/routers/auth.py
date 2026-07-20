from fastapi import APIRouter

# 도메인: auth (회원가입 / 로그인)
router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.get("/ping")
def ping():
    """
    auth 라우터가 정상 연결됐는지 확인하는 임시 테스트 엔드포인트.
    실제 API(회원가입, 로그인, 토큰 재발급 등) 구현하면서 이 함수는 지우면 됩니다.
    """
    return {"domain": "auth", "status": "ok"}
