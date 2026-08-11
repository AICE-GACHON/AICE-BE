import uuid
from datetime import datetime

from sqlalchemy import String, Integer, DateTime, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# openreview_id 자리표시자의 접두사. 베타에서는 가입 시 이 값을 받지 않는데
# 컬럼이 unique·not null이라 비워둘 수 없어서, 계정마다 고유한
# `pending:{uuid4}`를 넣는다 (app/routers/auth.py _openreview_id_or_placeholder).
#
# **이것은 DB 사정이지 API 계약이 아니다.** 밖으로 내보낼 때는 숨긴다
# (app/schemas/auth.py UserResponse). 만드는 쪽과 숨기는 쪽이 각자 문자열을
# 들고 있으면 한쪽만 바뀔 때 조용히 어긋나므로 — 화면에 `pending:9f3c…`가
# 그대로 찍혀도 아무도 예외를 못 본다 — 정의를 여기 한 곳에만 둔다.
OPENREVIEW_ID_PENDING_PREFIX = "pending:"


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

    @property
    def has_password(self) -> bool:
        """비밀번호로 로그인할 수 있는 계정인지. 계산 필드다 (컬럼 아님).

        **google_linked로는 이걸 알 수 없다.** 이메일로 가입한 뒤 같은 이메일로
        구글 로그인을 하면 google_sub가 채워지므로(routers/auth.py google_login),
        `google_linked=True`인데 비밀번호도 멀쩡히 있는 계정이 존재한다. 즉
        "구글 연동됨"과 "구글 전용(비밀번호 없음)"은 다른 말이다.

        프론트가 이 구분을 못 하면 두 화면이 동시에 틀어진다:
          - 비밀번호 변경 UI — 구글 전용 계정에 띄우면 400만 받고 끝난다
            (routers/user.py update_me). 반대로 숨기면 바꿀 수 있는 사람이 못 바꾼다.
          - 탈퇴 확인 — 비밀번호가 있는 계정은 body에 password가 **필수**다.
            안 보내면 400이고, 구글 전용 계정에 입력칸을 띄우면 채울 값이 없다.

        password_hash 자체는 절대 내보내지 않는다. 있고 없고만 알려준다.
        """
        return self.password_hash is not None
