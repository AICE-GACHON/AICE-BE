import uuid
from datetime import datetime

from sqlalchemy import String, Integer, DateTime, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class User(Base):
    """
    사용자(users) 테이블.
    이메일/비밀번호 또는 구글 로그인을 지원합니다 (카카오 로그인, 약관 동의 이력 등은
    이번 주제에서는 불필요해서 뺐습니다. 필요해지면 나중에 테이블을 추가하면 됩니다).
    """
    __tablename__ = "users"

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    # Google 로그인 전용 계정은 비밀번호가 없다.
    password_hash: Mapped[str | None] = mapped_column(String(500), nullable=True)
    nickname: Mapped[str] = mapped_column(String(50), nullable=False)
    # Google 계정 연동 식별자(OAuth sub). 이메일 가입 계정은 null이고, 나중에
    # 같은 이메일로 구글 로그인을 하면 채워진다(app/routers/auth.py google_login).
    google_sub: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    # 서비스 전체가 OpenReview 코퍼스 기반이라, 가입 경로(이메일/구글) 상관없이
    # 필수로 받는다 — 나중에 "내가 낸 논문" 매칭 등에 쓸 신원 값. 한 OpenReview
    # 계정이 여러 서비스 계정을 자처할 수 없도록 unique로 잠근다
    # (alembic/versions/0006_unique_openreview_id.py).
    openreview_id: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    # refresh_token 폐기용 버전 카운터. 로그아웃 시 1 증가시켜, 그 이전에 발급된
    # refresh_token(버전이 낮음)을 전부 무효화한다 (JWT는 상태가 없어 블랙리스트
    # 없이는 개별 폐기가 불가능하므로, 버전 비교로 대신한다).
    token_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
        nullable=False)

    @property
    def google_linked(self) -> bool:
        """UserResponse.model_validate(user)가 읽는 계산 필드. 실제 컬럼이 아니다."""
        return self.google_sub is not None
