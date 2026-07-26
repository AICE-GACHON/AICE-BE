"""Google 로그인 — 프론트가 Google Identity Services 등으로 받은 id_token을
서버에서 검증한다. 서버는 OAuth 리다이렉트 플로우를 직접 다루지 않는다(SPA 프론트
표준 패턴: 프론트가 구글과 직접 통신해 id_token을 받고, 백엔드는 그 토큰의 서명·
audience·만료만 확인한다).
"""
from google.auth.exceptions import GoogleAuthError
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token

from app.core.config import settings

_google_request = google_requests.Request()


def verify_google_id_token(token: str) -> dict:
    """id_token을 검증하고 클레임(sub, email, name 등)을 돌려준다.

    서명이 유효하지 않거나 audience(GOOGLE_CLIENT_ID)가 다르거나 만료됐거나
    구글 인증서 조회에 실패하면 ValueError를 던진다 — google-auth는 이 경우들을
    ValueError와 GoogleAuthError로 나눠 던지므로 여기서 하나로 합쳐, 호출하는 쪽
    (routers/auth.py)은 ValueError 하나만 401로 변환하면 되게 한다.
    """
    if not settings.GOOGLE_CLIENT_ID:
        raise ValueError("GOOGLE_CLIENT_ID가 설정되지 않았습니다.")
    try:
        return google_id_token.verify_oauth2_token(
            token, _google_request, audience=settings.GOOGLE_CLIENT_ID
        )
    except GoogleAuthError as e:
        raise ValueError(str(e)) from e
