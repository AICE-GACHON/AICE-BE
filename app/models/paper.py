import uuid
from datetime import datetime

from sqlalchemy import String, Integer, Text, DateTime, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Paper(Base):
    """
    기존에 공개된 논문(papers) 테이블.
    OpenReview API로 수집한 '이미 심사가 끝난' 논문들이 여기 쌓입니다.
    이 테이블이 유사 논문 검색의 대상(코퍼스)이 됩니다.
    """
    __tablename__ = "papers"

    paper_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # OpenReview 쪽 forum id. 같은 논문을 중복 수집하지 않기 위해 unique로 둡니다.
    external_id: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    abstract: Mapped[str] = mapped_column(Text, nullable=False)
    venue: Mapped[str] = mapped_column(String(100), nullable=False)  # 예: "ICLR 2024"
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    field: Mapped[str | None] = mapped_column(String(100), nullable=True)  # 예: "Machine Learning"
    pdf_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # TODO: pgvector 확장 설치 후 Vector(1536) 등으로 교체
    embedding: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
