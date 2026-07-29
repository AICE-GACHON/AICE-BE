import uuid
from datetime import datetime

from sqlalchemy import String, DateTime, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class OnboardingProfile(Base):
    """
    회원가입 전 익명 상태에서 저장하는 온보딩 답변(onboarding_profiles).

    세션 쿠키 없이 스테이트리스 구조를 유지하기 위해, 프론트가 익명으로 만든
    onboarding_id를 회원가입 요청(SignupRequest.onboarding_id)에 실어 보내면
    그때 user_id를 채워 연결한다 (app/routers/auth.py signup). 그전까지는
    user_id가 null인 "주인 없는" 행이다.
    """
    __tablename__ = "onboarding_profiles"

    onboarding_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id", ondelete="CASCADE"), unique=True, nullable=True
    )
    user_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    experience: Mapped[str | None] = mapped_column(String(50), nullable=True)
    purposes: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    fields: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    stage: Mapped[str | None] = mapped_column(String(50), nullable=True)
    venue: Mapped[str | None] = mapped_column(String(100), nullable=True)
    result_order: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
