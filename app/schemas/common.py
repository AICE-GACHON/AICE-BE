from datetime import datetime
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict


class ORMBase(BaseModel):
    """DB 모델(ORM 객체)을 그대로 읽어서 응답할 때 상속하는 베이스 스키마."""
    model_config = ConfigDict(from_attributes=True)


class TimestampMixin(BaseModel):
    created_at: datetime


class Message(BaseModel):
    """단순 메시지 응답 (로그아웃·탈퇴처럼 돌려줄 데이터가 안내 문구뿐일 때)."""
    message: str


DataT = TypeVar("DataT")


class ErrorDetail(BaseModel):
    code: str
    message: str


class ApiResponse(BaseModel, Generic[DataT]):
    """
    모든 API 응답을 감싸는 공통 포맷. 성공이면 data에 실제 응답을,
    실패면 error에 에러 정보를 채웁니다 (전역 에러 핸들러는 app/core/errors.py 참고).
    """
    success: bool = True
    data: DataT | None = None
    error: ErrorDetail | None = None
