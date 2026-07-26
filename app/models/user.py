import uuid
from datetime import datetime

from sqlalchemy import String, Integer, DateTime, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class User(Base):
    """
    사용자(users) 테이블.
    이메일/비밀번호 기반 간단 인증만 사용합니다 (카카오 로그인, 약관 동의 이력 등은
    이번 주제에서는 불필요해서 뺐습니다. 필요해지면 나중에 테이블을 추가하면 됩니다).
    """
    __tablename__ = "users"

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(500), nullable=False)
    nickname: Mapped[str] = mapped_column(String(50), nullable=False)
    # refresh_token 폐기용 버전 카운터. 로그아웃 시 1 증가시켜, 그 이전에 발급된
    # refresh_token(버전이 낮음)을 전부 무효화한다 (JWT는 상태가 없어 블랙리스트
    # 없이는 개별 폐기가 불가능하므로, 버전 비교로 대신한다).
    token_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
