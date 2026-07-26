import uuid
from datetime import datetime

from sqlalchemy import String, Integer, BigInteger, Text, DateTime, ForeignKey, Index, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Submission(Base):
    """
    사용자가 직접 올린 '내 논문 초안'(submissions) 테이블.

    코퍼스의 papers 테이블과 구조가 비슷해 보이지만 성격이 다릅니다 — papers는 이미
    심사가 끝난 공개 논문이고, submissions는 아직 리뷰를 받지 않은 사용자 소유의 초안입니다.

    임베딩 컬럼은 두지 않습니다. 분석할 때마다 paper_assistant가 SPECTER2로 계산하고
    버립니다 (저장하려면 vector(768)이어야 하는데, 재사용 이득보다 스키마 결합이 큽니다).
    """
    __tablename__ = "submissions"
    # 인덱스는 마이그레이션과 여기 양쪽에 있어야 한다. 모델에 없으면 alembic
    # autogenerate가 "모델에 없는 인덱스"로 보고 DROP을 제안한다.
    __table_args__ = (Index("submissions_user", "user_id"),)

    submission_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    abstract: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    field: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class SimilarPaperMatch(Base):
    """
    분석 1회분이 근거로 삼은 유사 논문 목록(similar_paper_matches).

    "이 예측이 어떤 논문을 보고 나왔는지"를 역방향으로도 조회할 수 있게 남깁니다
    (근거 추적 가능성 확보).

    ⚠️ 유사도 점수 컬럼이 없는 것은 빠뜨린 게 아니라 의도한 설계입니다. 검색 상위
    20편의 코사인 유사도 폭이 0.013이라 1위와 20위가 사실상 같은 값이고, 어떤 변환을
    해도 순위를 정당화할 점수가 나오지 않습니다. 그래서 rank(순위)와
    match_type(왜 걸렸는지)만 남깁니다 — 프론트에 "유사도 92%" 같은 UI를 만들면 안 됩니다.
    """
    __tablename__ = "similar_paper_matches"
    __table_args__ = (
        Index("similar_paper_matches_prediction", "prediction_id", "rank"),
        Index("similar_paper_matches_paper", "paper_id"),
    )

    match_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    prediction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("review_predictions.prediction_id", ondelete="CASCADE"),
        nullable=False,
    )
    # 코퍼스 papers.id (BIGINT). 코퍼스 테이블은 scripts/init_db.sql이 관리하므로
    # SQLAlchemy 모델도 FK 제약도 없습니다. 상세 조회는 paper_assistant로 합니다.
    paper_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    # both(의미+용어 모두) | semantic(의미만) | lexical(용어만)
    match_type: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
