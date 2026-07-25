import uuid

from app.schemas.common import ORMBase, TimestampMixin


class PaperResponse(ORMBase, TimestampMixin):
    paper_id: uuid.UUID
    external_id: str
    title: str
    abstract: str
    venue: str
    year: int
    field: str | None
    pdf_url: str | None
