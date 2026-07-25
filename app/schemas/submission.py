import uuid

from pydantic import BaseModel, Field

from app.schemas.common import ORMBase, TimestampMixin


class SubmissionCreate(BaseModel):
    title: str = Field(max_length=300)
    abstract: str
    content: str | None = None
    field: str | None = Field(default=None, max_length=100)


class SubmissionResponse(ORMBase, TimestampMixin):
    submission_id: uuid.UUID
    user_id: uuid.UUID
    title: str
    abstract: str
    content: str | None
    field: str | None


class SimilarPaperMatchResponse(ORMBase, TimestampMixin):
    match_id: uuid.UUID
    submission_id: uuid.UUID
    paper_id: uuid.UUID
    similarity_score: float
