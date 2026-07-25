from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ORMBase(BaseModel):
    """DB 모델(ORM 객체)을 그대로 읽어서 응답할 때 상속하는 베이스 스키마."""
    model_config = ConfigDict(from_attributes=True)


class TimestampMixin(BaseModel):
    created_at: datetime


class Message(BaseModel):
    """단순 메시지 응답 (ping, 성공 알림 등)."""
    message: str
