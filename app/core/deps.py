import uuid

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError
from sqlalchemy.orm import Session

from app.core.security import decode_token
from app.database import get_db
from app.models.user import User

# tokenUrl은 Swagger(/docs)의 "Authorize" 버튼이 참고하는 경로일 뿐,
# 실제 로그인은 app.routers.auth.login (JSON body)을 통해 이루어집니다.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")

# 토큰이 없어도 401을 던지지 않는 변형. **공개 엔드포인트인데 일부 동작만 로그인을
# 요구할 때** 쓴다 (예: /story?refresh=true — 조회는 누구나, 캐시 재생성은 회원만).
_optional_oauth2 = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)


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
        payload = decode_token(token)
        user_id = payload.get("sub")
        if user_id is None or payload.get("type") != "access":
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    try:
        user = db.get(User, uuid.UUID(user_id))
    except (ValueError, TypeError):
        # sub가 UUID 문자열이 아니면 ValueError, 문자열이 아예 아니면(숫자 등)
        # TypeError다. 후자를 놓치면 500이 난다 — routers/auth.py의 refresh는
        # 이미 둘 다 잡고 있어서, 여기만 다르면 같은 입력에 응답이 갈린다.
        raise credentials_exception

    if user is None:
        raise credentials_exception
    return user


def get_current_user_optional(
    token: str | None = Depends(_optional_oauth2),
    db: Session = Depends(get_db),
) -> User | None:
    """로그인했으면 User, 아니면 None. **인증 실패도 None이다 — 401을 던지지 않는다.**

    공개 엔드포인트 안에서 "비싼 동작만 회원 전용"으로 나눌 때 쓴다. 호출자가
    None을 받고 무엇을 할지(거부할지, 축소된 결과를 줄지) 직접 정해야 한다 —
    이 함수는 판단하지 않는다.
    """
    if token is None:
        return None
    try:
        return get_current_user(token=token, db=db)
    except HTTPException:
        # 만료·위조 토큰을 들고 온 경우. 공개 경로이므로 비로그인과 같게 취급한다.
        return None
