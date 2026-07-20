from fastapi import APIRouter

# 도메인: paper (OpenReview에서 수집한 기존 논문 코퍼스 조회/검색)
router = APIRouter(prefix="/api/papers", tags=["paper"])


@router.get("/ping")
def ping():
    """
    paper 라우터가 정상 연결됐는지 확인하는 임시 테스트 엔드포인트.
    실제 API(논문 목록/상세, 제목·초록 검색 등) 구현하면서 이 함수는 지우면 됩니다.
    """
    return {"domain": "paper", "status": "ok"}
