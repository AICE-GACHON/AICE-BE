import uuid
from datetime import datetime

from pydantic import Field

from app.schemas.common import ORMBase, TimestampMixin
from app.schemas.submission import SimilarPaperMatchResponse
from paper_assistant.schemas import Report


class AnalysisStartResponse(ORMBase, TimestampMixin):
    """분석 요청 직후 응답 (202). 아직 결과는 없습니다."""
    prediction_id: uuid.UUID
    submission_id: uuid.UUID
    status: str = Field(description="pending | running | done | failed")


class AnalysisResponse(ORMBase, TimestampMixin):
    """분석 조회 응답. status가 done일 때만 report가 채워집니다.

    프론트는 status=pending|running 동안 이 엔드포인트를 폴링하면 됩니다.

    ⚠️ report를 화면에 옮길 때 주의할 점은 docs/AI_파트_팀_공유.md §4에 정리돼 있습니다.
    특히 confidence.level이 weak이면 결과 위에 경고를 띄워야 하고, 유사 논문에
    유사도 %를 표시하면 안 됩니다.
    """
    prediction_id: uuid.UUID
    submission_id: uuid.UUID
    status: str
    report: Report | None = None
    confidence_level: str | None = None   # strong | moderate | weak
    is_reliable: bool | None = None
    explanation_source: str = Field(description="stub | llm — 실제 실행 결과")
    error: str | None = None
    completed_at: datetime | None = None
    matches: list[SimilarPaperMatchResponse] = []
