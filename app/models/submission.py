import uuid
from datetime import datetime

from sqlalchemy import String, Text, Numeric, DateTime, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Submission(Base):
    """
    사용자가 직접 올린 '내 논문 초안'(submissions) 테이블.
    papers 테이블과 구조는 비슷하지만, submissions는 아직 리뷰를 받지 않은
    사용자 소유의 초안이라는 점이 다릅니다.
    """
    __tablename__ = "submissions"

    submission_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    abstract: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    field: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # TODO: pgvector 확장 설치 후 Vector(1536) 등으로 교체
    embedding: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class SimilarPaperMatch(Base):
    """
    '내 논문(submission)과 비슷한 기존 논문(paper)' 검색 결과를 저장하는
    (similar_paper_matches) 테이블.
    나중에 review_predictions가 '어떤 근거로 이 예측을 했는지' 추적할 때
    이 테이블을 참조합니다 (근거 추적 가능성 확보).
    """
    __tablename__ = "similar_paper_matches"

    match_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    submission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("submissions.submission_id"), nullable=False
    )
    paper_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("papers.paper_id"), nullable=False)
    similarity_score: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False)  # 0.0000 ~ 1.0000
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
