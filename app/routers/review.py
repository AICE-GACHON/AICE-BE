import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.review import Review
from app.schemas.common import ApiResponse
from app.schemas.review import ReviewResponse

# 도메인: review (기존 논문이 받은 리뷰 / 리뷰 이후 수정 이력 조회)
router = APIRouter(prefix="/api/reviews", tags=["review"])


@router.get("", response_model=ApiResponse[list[ReviewResponse]])
def list_reviews(paper_id: uuid.UUID = Query(...), db: Session = Depends(get_db)):
    reviews = db.query(Review).filter(Review.paper_id == paper_id).all()
    return ApiResponse[list[ReviewResponse]](data=[ReviewResponse.model_validate(r) for r in reviews])
