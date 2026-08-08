from datetime import datetime, timedelta, timezone

from jose import jwt
from passlib.context import CryptContext

from app.core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, password_hash: str) -> bool:
    return pwd_context.verify(plain_password, password_hash)


# 존재하지 않는 계정에도 같은 시간을 쓰기 위한 미끼 해시. 모듈 로드 시 한 번만
# 만든다 (매 요청 hash()를 부르면 검증보다 오히려 더 느려서 차이가 반대로 난다).
_DUMMY_HASH = pwd_context.hash("aice-timing-equalizer")


def dummy_verify(plain_password: str) -> None:
    """실패가 확정된 로그인에서도 bcrypt를 한 번 돌려 응답 시간을 맞춘다.

    비밀번호 검증 자체가 느린 연산이라(bcrypt는 일부러 느리다), 계정이 없을 때만
    그 비용을 건너뛰면 응답 시간이 곧 "이 이메일이 가입돼 있는가"의 답이 된다.
    routers/auth.py의 login이 이 함수를 부르는 이유다.
    """
    pwd_context.verify(plain_password, _DUMMY_HASH)


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


# 비밀번호 재설정 토큰 수명. 메일함이 잠깐 열려 있어도 위험이 오래가지 않도록
# 짧게 잡는다. 사용자가 메일을 확인하고 새 비밀번호를 정하기에는 충분하다.
PASSWORD_RESET_EXPIRE_MINUTES = 30


def create_password_reset_token(subject: str, version: int) -> str:
    """비밀번호 재설정용 단기 토큰.

    refresh_token과 같은 방식으로 token_version 스냅샷을 담는다. 그래서
    **한 번 쓰면 못 쓴다** — 재설정이 성공하면 token_version이 올라가 이 토큰의
    ver과 어긋난다. 로그아웃이나 비밀번호 변경도 같은 이유로 이 토큰을 무효화한다.
    (별도 저장소 없이 일회용 성질을 얻는 방법이다.)
    """
    expire = datetime.now(timezone.utc) + timedelta(minutes=PASSWORD_RESET_EXPIRE_MINUTES)
    payload = {"sub": subject, "exp": expire, "type": "password_reset", "ver": version}
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    """
    JWT를 디코딩해서 payload를 돌려줍니다. 토큰이 만료됐거나 서명이 유효하지 않으면
    jose.JWTError를 그대로 던지므로, 호출하는 쪽에서 처리합니다. access/refresh
    공통이며, 어느 쪽인지는 payload["type"]으로 구분합니다.
    """
    return jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
