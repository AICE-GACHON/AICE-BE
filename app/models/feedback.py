import uuid
from datetime import datetime

from sqlalchemy import String, Text, JSON, DateTime, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ReviewPrediction(Base):
    """
    서비스의 핵심 산출물인 '예상 리뷰 및 수정 제안'(review_predictions) 테이블.

    설계 포인트: 이 결과가 '정답'처럼 보이면 안 됩니다 (멘토링에서 지적된
    RAG 오남용 문제). 그래서 based_on_matches에 근거로 쓴 similar_paper_matches의
    id 목록을 같이 저장해서, 사용자가 "이 예측이 어떤 유사 논문/리뷰를 근거로
    나왔는지" 항상 확인할 수 있게 합니다.
    """
    __tablename__ = "review_predictions"

    prediction_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    submission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("submissions.submission_id"), nullable=False
    )
    predicted_points: Mapped[str] = mapped_column(Text, nullable=False)  # 예상 리뷰 포인트 (사람이 읽을 텍스트)
    suggested_revision: Mapped[str | None] = mapped_column(Text, nullable=True)  # 수정 방향 제안
    based_on_matches: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # 근거로 쓴 match_id 목록 등
    explanation_source: Mapped[str] = mapped_column(String(20), nullable=False)  # "rule" | "rag" | "supervisor" 등
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
