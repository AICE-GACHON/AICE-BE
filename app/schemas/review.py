import uuid

from app.schemas.common import ORMBase, TimestampMixin


class ReviewResponse(ORMBase, TimestampMixin):
    review_id: uuid.UUID
    paper_id: uuid.UUID
    reviewer_label: str
    rating: int | None
    confidence: int | None
    content: str
    decision: str | None


class RevisionResponse(ORMBase, TimestampMixin):
    revision_id: uuid.UUID
    paper_id: uuid.UUID
    version_number: int
    change_summary: str | None
    content: str | None
