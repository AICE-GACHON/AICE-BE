import uuid
from datetime import datetime

from sqlalchemy import String, Integer, DateTime, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class CodefConnection(Base):
    """CODEF연동정보(codef_connections) 테이블 - 카드사 계정 연동 정보"""
    __tablename__ = "codef_connections"

    connection_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False)
    connected_id: Mapped[str] = mapped_column(String(100), nullable=False)  # CODEF connectedId
    issuer_code: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    connected_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class CardPerformance(Base):
    """실적진행상황(card_performances) 테이블 - 카드별 이번 달 실적"""
    __tablename__ = "card_performances"

    performance_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_card_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user_cards.user_card_id"), unique=True, nullable=False
    )
    month_spend_amount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    input_type: Mapped[str] = mapped_column(String(20), nullable=False)  # 'auto' or 'manual'
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
