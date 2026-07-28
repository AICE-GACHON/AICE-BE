import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Submission(Base):
    """사용자가 직접 올린 '내 논문 초안'(submissions) 테이블.

    코퍼스의 papers 테이블과 구조가 비슷해 보이지만 성격이 다릅니다 — papers는 이미
    심사가 끝난 공개 논문이고, submissions는 아직 리뷰를 받지 않은 사용자 소유의 초안입니다.

    임베딩 컬럼은 두지 않습니다. 분석할 때마다 paper_assistant가 SPECTER2로 계산하고
    버립니다 (저장하려면 vector(768)이어야 하는데, 재사용 이득보다 스키마 결합이 큽니다).
    """
    __tablename__ = "submissions"
    # 인덱스는 마이그레이션과 여기 양쪽에 있어야 한다. 모델에 없으면 alembic
    # autogenerate가 "모델에 없는 인덱스"로 보고 DROP을 제안한다.
    __table_args__ = (Index("submissions_user", "user_id", "created_at"),)

    submission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    abstract: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    field: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
