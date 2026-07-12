import uuid
from datetime import datetime

from sqlalchemy import String, Integer, DateTime, ForeignKey, func, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Card(Base):
    """카드카탈로그(cards) 테이블 - MVP 14장 카드 목록"""
    __tablename__ = "cards"

    card_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    issuer: Mapped[str] = mapped_column(String(50), nullable=False)
    annual_fee: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    highlight: Mapped[str | None] = mapped_column(String(100), nullable=True)


class UserCard(Base):
    """보유카드(user_cards) 테이블 - 사용자-카드 다대다 연결 테이블"""
    __tablename__ = "user_cards"
    __table_args__ = (UniqueConstraint("user_id", "card_id", name="uq_user_card"),)

    user_card_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False)
    card_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("cards.card_id"), nullable=False)
    registered_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
