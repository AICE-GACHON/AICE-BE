import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, func)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# 아직 끝나지 않은 분석 상태. DB의 부분 유니크 인덱스 조건과 반드시 같아야 한다
# (alembic 0002) — 여기만 바꾸면 중복 방지가 조용히 풀린다.
IN_PROGRESS = ("pending", "running")


class ReviewPrediction(Base):
    """분석 1회분(review_predictions). 백그라운드 작업의 상태이자 결과 저장소입니다.

    분석은 paper_assistant.analyze() 호출 하나로 끝나지만 SPECTER2 로드 + 검색 +
    집계에 수 초~수십 초가 걸려서 요청 안에서 동기로 돌릴 수 없습니다. 그래서
    POST가 pending 행을 만들고 202로 돌아온 뒤, 백그라운드에서 status를 running →
    done/failed로 옮기며 report를 채웁니다.

    설계 포인트: 결과가 '정답'처럼 보이면 안 됩니다 (1차 멘토링의 RAG 오남용 지적).
    그래서 (1) 근거로 쓴 유사 논문을 similar_paper_matches에 남기고, (2) 결과를 어떻게
    만들었는지를 explanation_source에, (3) 검색 결과 자체를 믿어도 되는지를
    confidence_level/is_reliable에 담아 항상 함께 내려줍니다.

    ⚠️ 한 submission에 진행 중(pending|running)인 분석은 **DB 레벨에서 하나만**
    허용됩니다 (부분 유니크 인덱스). 동시 요청이 들어와도 중복 실행되지 않습니다.
    """
    __tablename__ = "review_predictions"
    __table_args__ = (
        Index("review_predictions_submission", "submission_id", "created_at"),
    )

    prediction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    submission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("submissions.submission_id", ondelete="CASCADE"),
        nullable=False,
    )
    # pending -> running -> done | failed
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    # paper_assistant.schemas.Report 전체를 그대로 담습니다. 섹션별 컬럼으로 쪼개지
    # 않은 이유: Report에는 신뢰도·lift·p값·학회 경향·점수 분포가 중첩 구조로 들어 있고,
    # AI 파트가 필드를 늘릴 때마다 마이그레이션을 만들어야 하기 때문입니다.
    report: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # 아래 둘은 report 안에도 있지만 목록 조회에서 JSONB를 매번 파싱하지 않도록 꺼내 둔 사본.
    confidence_level: Mapped[str | None] = mapped_column(String(20), nullable=True)
    is_reliable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # stub = LLM 없이 규칙/통계만 ($0, 기본) | llm = Haiku/Sonnet 실제 호출.
    # 설정값이 아니라 Report.used_llm(실행 결과)에서 옮겨 적습니다.
    explanation_source: Mapped[str] = mapped_column(
        String(20), nullable=False, default="stub")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)


class SimilarPaperMatch(Base):
    """분석 1회분이 근거로 삼은 유사 논문 목록(similar_paper_matches).

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

    match_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
