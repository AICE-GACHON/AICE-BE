from fastapi import APIRouter, Depends, HTTPException, status

from app.core.deps import get_current_user
from app.models.user import User
from app.schemas.feedback import ReviewPredictionRequest, ReviewPredictionResponse

# 도메인: feedback (핵심 기능 - 유사 논문 검색 + 예상 리뷰/수정 제안)
router = APIRouter(prefix="/api/feedback", tags=["feedback"])


@router.post("/predictions", response_model=ReviewPredictionResponse, status_code=status.HTTP_201_CREATED)
def create_prediction(
    payload: ReviewPredictionRequest,
    current_user: User = Depends(get_current_user),
):
    """
    submission을 기반으로 유사 논문 검색(similar_paper_matches) + 예상 리뷰/수정 제안
    (review_predictions)을 생성하는 핵심 엔드포인트. 아직 뼈대만 있는 상태입니다.

    TODO:
    - submission 소유자 검증
    - 유사 논문 검색 (임베딩 + 벡터 검색) 실행/조회
    - 서브 RAG + 슈퍼바이저 에이전트로 예상 리뷰/수정 제안 생성
    - review_predictions 저장 (based_on_matches에 근거 match_id 기록)
    """
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="아직 구현되지 않았습니다.")
