import uuid
from datetime import datetime

from sqlalchemy import String, Numeric, Integer, Boolean, DateTime, ForeignKey, JSON, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class BenefitClause(Base):
    """
    혜택절(benefit_clauses) 테이블 - RAG 검색 대상.
    embedding 컬럼은 나중에 pgvector 확장 설치 후 Vector 타입으로 교체 예정.
    지금은 개발 초기 단계라 우선 text로 둡니다.
    """
    __tablename__ = "benefit_clauses"

    benefit_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    card_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("cards.card_id"), nullable=False)
    category: Mapped[str] = mapped_column(String(20), nullable=False)
    benefit_type: Mapped[str] = mapped_column(String(20), nullable=False)  # '적립' or '청구할인'
    rate: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False)
    monthly_cap: Mapped[int | None] = mapped_column(Integer, nullable=True)
    min_spend: Mapped[int] = mapped_column(Integer, default=0)
    include_notes: Mapped[str | None] = mapped_column(String(500), nullable=True)
    exclude_notes: Mapped[str | None] = mapped_column(String(500), nullable=True)
    embedding: Mapped[str | None] = mapped_column(String, nullable=True)  # TODO: pgvector 도입 시 Vector(1024)로 교체


class Recommendation(Base):
    """추천결과(recommendations) 테이블"""
    __tablename__ = "recommendations"

    recommendation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False)
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("merchants.merchant_id"), nullable=False
    )
    card_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("cards.card_id"), nullable=False)
    expected_benefit_won: Mapped[int] = mapped_column(Integer, nullable=False)
    eligible: Mapped[bool] = mapped_column(Boolean, default=True)
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    caveats: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    explanation_source: Mapped[str] = mapped_column(String(20), nullable=False)  # precomputed/llm_realtime/rule_only
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
