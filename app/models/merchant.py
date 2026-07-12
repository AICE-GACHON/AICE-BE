import uuid

from sqlalchemy import String, Numeric, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class CategoryMapping(Base):
    """업종매핑(category_mappings) 테이블 - Kakao 카테고리코드 <-> 내부 업종코드"""
    __tablename__ = "category_mappings"

    kakao_category_code: Mapped[str] = mapped_column(String(20), primary_key=True)
    internal_category_code: Mapped[str] = mapped_column(String(20), nullable=False)
    mapping_type: Mapped[str] = mapped_column(String(20), nullable=False)  # 'code' or 'embedding_fallback'


class Merchant(Base):
    """가맹점(merchants) 테이블 - Kakao Local API 조회 결과 캐시"""
    __tablename__ = "merchants"

    merchant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    latitude: Mapped[float] = mapped_column(Numeric(10, 7), nullable=False)
    longitude: Mapped[float] = mapped_column(Numeric(10, 7), nullable=False)
    kakao_category_code: Mapped[str] = mapped_column(
        String(20), ForeignKey("category_mappings.kakao_category_code"), nullable=False
    )
