from datetime import datetime, timedelta, timezone

from jose import jwt
from passlib.context import CryptContext

from app.core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, password_hash: str) -> bool:
    return pwd_context.verify(plain_password, password_hash)


def create_access_token(subject: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {"sub": subject, "exp": expire, "type": "access"}
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_refresh_token(subject: str, version: int) -> str:
    """
    refresh_token은 access_token보다 수명이 길어서(기본 14일), 로그아웃 시 폐기할
    방법이 필요합니다. version에 User.token_version 스냅샷을 담아두고, 로그아웃 시
    token_version을 올려서 그 이전에 발급된 refresh_token을 전부 무효화합니다.
    """
    expire = datetime.now(timezone.utc) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    payload = {"sub": subject, "exp": expire, "type": "refresh", "ver": version}
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    """
    JWT를 디코딩해서 payload를 돌려줍니다. 토큰이 만료됐거나 서명이 유효하지 않으면
    jose.JWTError를 그대로 던지므로, 호출하는 쪽에서 처리합니다. access/refresh
    공통이며, 어느 쪽인지는 payload["type"]으로 구분합니다.
    """
    return jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
