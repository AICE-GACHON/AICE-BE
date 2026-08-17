import uuid
from datetime import datetime, timezone

from sqlalchemy import String, Integer, DateTime, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.legal import PRIVACY_VERSION, TERMS_VERSION
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
    이메일/비밀번호 또는 구글 로그인을 지원합니다 (카카오 로그인 등은 이번 주제에서는
    불필요해서 뺐습니다. 필요해지면 나중에 테이블을 추가하면 됩니다).

    약관 동의는 이력 테이블이 아니라 **현재 동의 상태**만 컬럼으로 들고 있습니다
    (terms_agreed_at / terms_version / privacy_version). 재동의 때마다 덮어씁니다 —
    "언제 어느 버전에 동의했는가"를 증명하는 데는 최신 한 건이면 충분하고, 과거
    이력까지 남겨야 할 요구가 생기면 그때 별도 테이블로 옮기는 편이 낫습니다.
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

    # 약관·개인정보처리방침 동의 이력. 셋 다 **nullable이고, 그게 의도다** —
    # 이 컬럼들이 생기기 전에 가입한 계정은 동의를 받은 적이 없다. 마이그레이션에서
    # 기본값으로 채우면 받지도 않은 동의를 받았다고 기록하는 셈이라, 이력을 남기는
    # 목적 자체를 배반한다. null은 "이력 없음"이고, 그 계정은 consent_up_to_date가
    # False라서 프론트가 재동의를 받게 된다.
    terms_agreed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    # 동의 시점에 게시돼 있던 버전 (app/core/legal.py). 문서를 고치고 버전을 올리면
    # 여기 값과 달라져 재동의 대상이 된다.
    terms_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    privacy_version: Mapped[str | None] = mapped_column(String(20), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
        nullable=False)

    def record_consent(self) -> None:
        """지금 게시 중인 버전에 동의했음을 기록합니다.

        **시각만이 아니라 버전을 함께 찍는 것이 핵심입니다.** 시각만 남기면 문서를
        한 번 개정한 순간부터 "2026-08-16에 동의함"이 어느 문구에 대한 동의인지
        말해주지 못합니다.

        가입(routers/auth.py)과 재동의(routers/user.py) 두 경로가 이 메서드를
        공유합니다. 각자 세 줄씩 들고 있으면 나중에 항목이 하나 늘 때 한쪽만
        고쳐지고, 그 사실은 분쟁이 나서 이력을 꺼내볼 때까지 아무도 모릅니다.
        """
        self.terms_agreed_at = datetime.now(timezone.utc)
        self.terms_version = TERMS_VERSION
        self.privacy_version = PRIVACY_VERSION

    @property
    def consent_up_to_date(self) -> bool:
        """현재 게시 중인 약관·처리방침에 동의한 상태인지. 계산 필드입니다 (컬럼 아님).

        **프론트가 버전 문자열을 직접 들고 비교하지 않게 하려고 서버가 판정합니다.**
        프론트에 "1.0"을 박아두면 문서를 고치고 legal.py만 올렸을 때 재동의 화면이
        안 뜨고, 그러면 아무도 오류를 못 봅니다 — 사용자는 옛 문구에 동의한 채로
        계속 쓰고, 서버는 새 문구를 게시 중이라고 믿습니다.

        False인 경우는 둘입니다: 동의 이력이 아예 없는 계정(컬럼 도입 전 가입)과,
        동의는 했지만 그 뒤 문서가 개정된 계정. 프론트 입장에서는 둘 다 "재동의를
        받아야 한다"로 같으므로 구분해서 내보내지 않습니다.
        """
        return (self.terms_version == TERMS_VERSION
                and self.privacy_version == PRIVACY_VERSION)

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
