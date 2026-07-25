import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.paper import Paper
from app.schemas.paper import PaperResponse

# 도메인: paper (OpenReview에서 수집한 기존 논문 코퍼스 조회/검색)
router = APIRouter(prefix="/api/papers", tags=["paper"])


@router.get("/{paper_id}", response_model=PaperResponse)
def get_paper(paper_id: uuid.UUID, db: Session = Depends(get_db)):
    paper = db.get(Paper, paper_id)
    if paper is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="논문을 찾을 수 없습니다.")
    return paper
