import uuid
from datetime import datetime

from sqlalchemy import String, Integer, Text, DateTime, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Review(Base):
    """
    기존 논문(papers)이 실제로 받은 리뷰(reviews) 테이블.
    OpenReview에는 논문 하나에 리뷰가 여러 개 달리므로 1:N 관계입니다.
    """
    __tablename__ = "reviews"

    review_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    paper_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("papers.paper_id"), nullable=False)
    reviewer_label: Mapped[str] = mapped_column(String(50), nullable=False)  # 예: "Reviewer 1" (실명 아님)
    rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confidence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    decision: Mapped[str | None] = mapped_column(String(30), nullable=True)  # 예: "Accept", "Reject"
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Revision(Base):
    """
    논문이 리뷰를 받은 뒤 어떻게 수정됐는지 버전별로 기록하는(revisions) 테이블.
    '유사 논문이 리뷰 이후 어떻게 수정되었는지' 기능의 핵심 데이터입니다.
    """
    __tablename__ = "revisions"

    revision_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    paper_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("papers.paper_id"), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)  # 1, 2, 3 ...
    change_summary: Mapped[str | None] = mapped_column(Text, nullable=True)  # 무엇을 왜 고쳤는지 요약
    content: Mapped[str | None] = mapped_column(Text, nullable=True)  # 해당 버전의 abstract/본문 전체 또는 일부
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
