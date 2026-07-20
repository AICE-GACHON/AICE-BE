from fastapi import APIRouter

# 도메인: submission (사용자가 올린 내 논문 초안 업로드/조회)
router = APIRouter(prefix="/api/submissions", tags=["submission"])


@router.get("/ping")
def ping():
    """
    submission 라우터가 정상 연결됐는지 확인하는 임시 테스트 엔드포인트.
    실제 API(초안 업로드, 목록/상세 조회 등) 구현하면서 이 함수는 지우면 됩니다.
    """
    return {"domain": "submission", "status": "ok"}
