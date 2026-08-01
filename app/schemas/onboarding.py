import uuid

from pydantic import BaseModel

from app.schemas.common import ORMBase, TimestampMixin


class OnboardingCreate(BaseModel):
    user_type: str | None = None
    experience: str | None = None
    purposes: list[str] = []
    fields: list[str] = []
    stage: str | None = None
    venue: str | None = None
    result_order: list[str] = []


class OnboardingResponse(ORMBase, TimestampMixin):
    onboarding_id: uuid.UUID
    user_type: str | None
    experience: str | None
    purposes: list[str]
    fields: list[str]
    stage: str | None
    venue: str | None
    result_order: list[str]
