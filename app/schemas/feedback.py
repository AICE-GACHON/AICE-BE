import uuid

from pydantic import BaseModel

from app.schemas.common import ORMBase, TimestampMixin


class ReviewPredictionRequest(BaseModel):
    submission_id: uuid.UUID


class ReviewPredictionResponse(ORMBase, TimestampMixin):
    prediction_id: uuid.UUID
    submission_id: uuid.UUID
    predicted_points: str
    suggested_revision: str | None
    based_on_matches: dict | None
    explanation_source: str
