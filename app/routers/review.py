from fastapi import APIRouter

# 도메인: review (기존 논문이 받은 리뷰 / 리뷰 이후 수정 이력 조회)
# 실제 API 명세서를 확정하면 prefix가 "/api/papers/{paper_id}/reviews"처럼
# paper 도메인 하위 경로로 바뀔 가능성이 높습니다. 지금은 paper.router와 경로가
# 겹치지 않도록 임시로 별도 prefix를 씁니다.
router = APIRouter(prefix="/api/reviews", tags=["review"])


@router.get("/ping")
def ping():
    """
    review 라우터가 정상 연결됐는지 확인하는 임시 테스트 엔드포인트.
    실제 API(특정 논문의 리뷰 목록, 버전별 수정 이력 조회 등) 구현하면서
    이 함수는 지우면 됩니다.
    """
    return {"domain": "review", "status": "ok"}
