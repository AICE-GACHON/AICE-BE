import secrets
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# 토큰 바이트 수. token_urlsafe(32)는 base64url 43자가 되고 엔트로피는 256비트다.
# 이 값이 곧 공개 링크의 유일한 자물쇠라는 점이 중요하다 — 인증이 없으므로 토큰을
# 맞히면 그대로 열린다. 대입 시도는 rate limit이 막지만(routers/shared.py), 상한을
# 우회당하더라도 32바이트를 훑는 것은 불가능하다.
_TOKEN_BYTES = 32


def new_token() -> str:
    """추측 불가능한 공유 토큰.

    ⚠️ **`random` 모듈로 바꾸지 마세요.** Mersenne Twister는 출력 몇 개만 보면
    내부 상태를 복원할 수 있어서, 자기 공유 링크 몇 개로 남의 토큰을 계산할 수
    있게 됩니다. `secrets`는 OS CSPRNG를 씁니다.
    """
    return secrets.token_urlsafe(_TOKEN_BYTES)


class SubmissionShare(Base):
    """분석 결과의 공개 공유 링크(submission_shares).

    기존 결과 화면 경로는 로그인이 필요해서, 링크를 받은 사람도 그 계정으로
    로그인해야만 열 수 있었다. 이 테이블은 **소유자가 명시적으로 발급한 토큰**
    하나로 그 벽을 우회하게 해준다 — 토큰을 아는 사람은 로그인 없이 결과를 본다.

    행을 지우지 않고 revoked_at을 찍는 이유: 공유를 되돌린 사실 자체가 기록이다.
    지워버리면 "이 링크가 언제까지 살아 있었나"에 답할 수 없고, 같은 토큰이 다시
    발급될 여지도 생긴다(unique 제약이 지워진 값을 막지 못하므로).

    ⚠️ **submission 하나에 살아 있는 토큰은 DB 레벨에서 하나뿐이다**(아래 부분
    유니크 인덱스). 프론트가 필요로 하는 것은 "현재 공유 URL 1개"이고, 여러 개를
    허용하면 폐기 API가 "무엇을 폐기할지"를 받아야 한다. 동시 요청이 들어와도
    두 개가 생기지 않는다 — review_predictions_one_active와 같은 장치다.
    """
    __tablename__ = "submission_shares"
    # 인덱스는 마이그레이션과 여기 양쪽에 있어야 한다. 모델에 없으면 alembic
    # autogenerate가 "모델에 없는 인덱스"로 보고 DROP을 제안한다
    # (app/models/analysis.py에도 같은 취지의 경고가 있다).
    __table_args__ = (
        # ⚠️ 위 docstring이 말하는 그 부분 유니크 인덱스다. **실제로 만드는 것은
        # alembic 0018이고, 여기 선언은 "모델이 DB를 설명하게" 하기 위한 것이다.**
        # 이 선언을 지우고 autogenerate가 제안하는 DROP을 무심코 적용하면, 한
        # submission에 활성 토큰이 여러 개 생기고 DELETE가 그중 하나만 폐기한다.
        Index("submission_shares_one_active", "submission_id", unique=True,
              postgresql_where=text("revoked_at IS NULL")),
    )

    share_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    submission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("submissions.submission_id", ondelete="CASCADE"),
        nullable=False,
    )
    # 공개 조회의 유일한 열쇠. unique=True가 인덱스도 겸하므로 따로 걸지 않는다.
    # 길이 128은 token_urlsafe(32)의 43자에 여유를 둔 값이다 — _TOKEN_BYTES를
    # 키우더라도 마이그레이션 없이 들어간다.
    token: Mapped[str] = mapped_column(
        String(128), nullable=False, unique=True, default=new_token)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    # NULL이면 살아 있는 공유. 값이 있으면 폐기된 것이고 공개 조회는 404가 된다.
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
