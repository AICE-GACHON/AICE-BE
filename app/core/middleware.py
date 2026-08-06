"""전역 미들웨어 — 응답 보안 헤더와 요청 본문 상한.

라우터마다 챙길 수 없는 두 가지를 여기서 한 번에 건다. 둘 다 "빠뜨리면 조용히
안전하지 않아지는" 종류라, 개별 엔드포인트의 성실함에 기대지 않는다.
"""
import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

log = logging.getLogger(__name__)

# 이 API는 JSON만 돌려주고 브라우저가 렌더할 HTML을 서빙하지 않는다. 그래서
# 헤더 구성이 단순하다 — 아래 값들은 "이 응답으로 브라우저가 할 수 있는 일"을
# 최소로 줄인다.
_BASE_HEADERS = {
    # 응답을 선언한 Content-Type이 아닌 것으로 추측해 실행하지 못하게 한다
    # (JSON 안에 심어둔 스크립트가 text/html로 해석되는 고전적 경로를 막는다).
    "X-Content-Type-Options": "nosniff",
    # API 응답을 iframe에 얹어 클릭재킹에 쓰는 것을 막는다.
    "X-Frame-Options": "DENY",
    # 외부로 나갈 때 경로·쿼리를 흘리지 않는다. 논문 id 같은 값이 Referer로
    # 새 나가는 것을 막는다.
    "Referrer-Policy": "no-referrer",
    # API에는 필요 없는 브라우저 기능을 통째로 끈다.
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    # /docs를 열어둔 경우를 포함해, 응답이 문서로 해석되더라도 외부 자원을
    # 끌어오거나 iframe에 담기지 못하게 한다.
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """모든 응답에 보안 헤더를 붙인다.

    HSTS는 https로 서비스할 때만 켠다(`enable_hsts`). http로 개발하는 중에 켜면
    브라우저가 localhost를 https로 기억해버려서, 그 뒤로 개발 서버에 접속이 안
    되는 상태가 캐시 수명만큼 이어진다 — 원인을 찾기 어려운 종류의 사고다.
    """

    def __init__(self, app, enable_hsts: bool = False, enable_docs: bool = False):
        super().__init__(app)
        self._headers = dict(_BASE_HEADERS)
        if enable_hsts:
            self._headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains")
        if enable_docs:
            # Swagger UI는 CDN 스크립트/스타일과 인라인 초기화를 쓴다. 기본 CSP를
            # 그대로 두면 /docs가 빈 화면이 된다. 문서를 연 경우에만 완화한다.
            self._docs_csp = (
                "default-src 'none'; "
                "script-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
                "style-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
                "img-src 'self' https://fastapi.tiangolo.com data:; "
                "connect-src 'self'; frame-ancestors 'none'")
        else:
            self._docs_csp = None

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        for name, value in self._headers.items():
            response.headers.setdefault(name, value)
        if self._docs_csp and request.url.path in ("/docs", "/redoc"):
            response.headers["Content-Security-Policy"] = self._docs_csp
        return response


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Content-Length가 상한을 넘는 요청을 본문을 읽기 전에 413으로 끊는다.

    **이것만으로는 충분하지 않다.** Content-Length는 클라이언트가 보내는 값이고,
    chunked 전송에는 아예 없다. 그래서 실제로 바이트를 세는 방어는 본문을 읽는
    쪽(app/routers/submissions.py의 PDF 업로드)에 따로 있다. 여기서 거르는 것은
    "정직하게 큰 요청"이고, 그것만으로도 서버가 24MB짜리 JSON을 파싱하느라
    메모리를 쓰는 일은 사라진다.
    """

    def __init__(self, app, max_bytes: int):
        super().__init__(app)
        self.max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next) -> Response:
        raw = request.headers.get("content-length")
        if raw is not None:
            try:
                declared = int(raw)
            except ValueError:
                declared = None
            if declared is not None and declared > self.max_bytes:
                log.warning("본문 상한 초과로 거부: %s %s (%d bytes)",
                            request.method, request.url.path, declared)
                return JSONResponse(
                    status_code=413,
                    content={
                        "success": False,
                        "data": None,
                        "error": {"code": "413", "message": "요청 본문이 너무 큽니다."},
                    })
        return await call_next(request)
