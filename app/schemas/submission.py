import uuid

from pydantic import BaseModel, Field

from app.schemas.common import ORMBase, TimestampMixin


class SubmissionCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    abstract: str = Field(min_length=1)
    content: str | None = None
    field: str | None = Field(default=None, max_length=100)


class SubmissionResponse(ORMBase, TimestampMixin):
    submission_id: uuid.UUID
    user_id: uuid.UUID
    title: str
    abstract: str
    content: str | None
    field: str | None


class SubmissionSummary(ORMBase, TimestampMixin):
    """목록 응답 — 본문(content)과 초록을 싣지 않아 가볍습니다."""
    submission_id: uuid.UUID
    title: str
    field: str | None


class SimilarPaperMatchResponse(ORMBase, TimestampMixin):
    """분석이 근거로 삼은 유사 논문 1편.

    similarity_score가 없는 것은 의도한 설계입니다 (app/models/analysis.py 주석 참고).
    상세 정보는 paper_id로 GET /api/papers/{paper_id}를 호출해 가져옵니다.
    """
    match_id: uuid.UUID
    prediction_id: uuid.UUID
    paper_id: int          # 코퍼스 papers.id (BIGINT)
    rank: int
    match_type: str        # both | semantic | lexical
