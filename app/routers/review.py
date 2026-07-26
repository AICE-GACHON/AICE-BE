from fastapi import APIRouter, HTTPException, Query, status

from app.schemas.common import ApiResponse
from app.schemas.review import ReviewResponse
from paper_assistant import get_paper_detail

# 도메인: review (기존 논문이 받은 리뷰 조회)
router = APIRouter(prefix="/api/reviews", tags=["review"])


@router.get("", response_model=ApiResponse[list[ReviewResponse]])
def list_reviews(paper_id: int = Query(..., description="코퍼스 papers.id (BIGINT)")):
    """특정 논문이 받은 리뷰 목록 (점수 높은 순, 점수 없는 리뷰는 뒤로).

    GET /api/papers/{paper_id} 응답에도 같은 리뷰가 들어 있습니다. 리뷰만 필요한
    화면을 위해 남겨둔 경로입니다.

    ⚠️ 2023년 이전 학회 리뷰는 강점/약점이 분리되지 않은 형식이라 weaknesses에 리뷰
    본문 전체가 들어옵니다. 이때 is_unsplit=true이므로, 프론트는 '약점'이라고 라벨을
    붙이지 말고 '리뷰 본문' 한 덩어리로 표시해야 합니다.
    """
    detail = get_paper_detail(paper_id)
    if detail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="논문을 찾을 수 없습니다.")
    return ApiResponse[list[ReviewResponse]](data=detail.reviews)
