import uuid

from pydantic import BaseModel, EmailStr, Field

from app.schemas.common import ORMBase, TimestampMixin


class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    nickname: str = Field(min_length=1, max_length=50)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserResponse(ORMBase, TimestampMixin):
    user_id: uuid.UUID
    email: EmailStr
    nickname: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
