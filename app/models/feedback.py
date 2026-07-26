import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import String, Text, Boolean, DateTime, ForeignKey, Index, func
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ReviewPrediction(Base):
    """
    분석 1회분(review_predictions). 백그라운드 작업의 상태이자 결과 저장소입니다.

    분석은 paper_assistant.analyze() 호출 하나로 끝나지만 SPECTER2 로드 + 검색 +
    집계에 수 초~수십 초가 걸려서 요청 안에서 동기로 돌릴 수 없습니다. 그래서
    POST가 pending 행을 만들고 202로 돌아온 뒤, 백그라운드에서 status를 running →
    done/failed로 옮기며 report를 채웁니다.

    설계 포인트: 결과가 '정답'처럼 보이면 안 됩니다 (1차 멘토링의 RAG 오남용 지적).
    그래서 (1) 근거로 쓴 유사 논문을 similar_paper_matches에 남기고, (2) 결과를 어떻게
    만들었는지를 explanation_source에, (3) 검색 결과 자체를 믿어도 되는지를
    confidence_level/is_reliable에 담아 항상 함께 내려줍니다.
    """
    __tablename__ = "review_predictions"
    __table_args__ = (
        Index("review_predictions_submission", "submission_id", "created_at"),
    )

    prediction_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
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
    confidence_level: Mapped[str | None] = mapped_column(String(20), nullable=True)  # strong|moderate|weak
    is_reliable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # stub = LLM 없이 규칙/통계만 ($0, 기본) | llm = Haiku/Sonnet 실제 호출
    explanation_source: Mapped[str] = mapped_column(String(20), nullable=False, default="stub")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
