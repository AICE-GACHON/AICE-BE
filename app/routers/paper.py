from fastapi import APIRouter, HTTPException, status

from app.schemas.common import ApiResponse
from app.schemas.paper import PaperResponse
from paper_assistant import get_paper_detail

# 도메인: paper (OpenReview에서 수집한 기존 논문 코퍼스 조회)
router = APIRouter(prefix="/api/papers", tags=["paper"])


@router.get("/{paper_id}", response_model=ApiResponse[PaperResponse])
def get_paper(paper_id: int):
    """유사 논문 1편의 상세 — 초록·저자·메타리뷰·리뷰 전문·지적 항목.

    코퍼스 테이블은 AI 파트가 소유하므로 SQLAlchemy가 아니라 paper_assistant를 통해
    조회합니다. 임베딩 모델을 쓰지 않는 순수 조회라 빠릅니다.

    ⚠️ paper_id는 UUID가 아니라 BIGINT입니다. 분석 결과의
    similar_papers[].paper_id를 그대로 넘기면 됩니다.
    """
    detail = get_paper_detail(paper_id)
    if detail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="논문을 찾을 수 없습니다.")
    return ApiResponse[PaperResponse](data=detail)
