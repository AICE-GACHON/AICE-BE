import uuid

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError
from sqlalchemy.orm import Session

from app.core.security import decode_access_token
from app.database import get_db
from app.models.user import User

# tokenUrl은 Swagger(/docs)의 "Authorize" 버튼이 참고하는 경로일 뿐,
# 실제 로그인은 app.routers.auth.login (JSON body)을 통해 이루어집니다.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    """
    Authorization: Bearer <token> 헤더를 검증하고, 그 안의 sub(user_id)에 해당하는
    User를 DB에서 조회해서 돌려주는 의존성. 로그인이 필요한 라우터에서 Depends로 사용합니다.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="인증 정보가 유효하지 않습니다.",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = decode_access_token(token)
        user_id = payload.get("sub")
        if user_id is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    try:
        user = db.get(User, uuid.UUID(user_id))
    except ValueError:
        raise credentials_exception

    if user is None:
        raise credentials_exception
    return user
