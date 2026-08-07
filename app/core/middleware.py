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


def _refuse(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "success": False,
            "data": None,
            "error": {"code": str(status_code), "message": message},
        })


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """본문 길이가 상한을 넘는 요청을 **본문을 읽기 전에** 끊는다.

    Content-Length가 상한을 넘으면 413이다. 여기까지는 자명하다.

    문제는 Content-Length가 **없는** 경우다. chunked 전송에는 그 헤더가 아예 없고,
    그러면 길이를 미리 알 방법이 없다. 예전에는 "실제로 바이트를 세는 방어가 본문을
    읽는 쪽에 따로 있다"고 적어두고 통과시켰지만, 그건 사실이 아니었다 — FastAPI는
    엔드포인트 함수가 시작되기 **전에** File(...) 의존성을 풀면서 multipart 본문을
    통째로 파싱해 임시파일(1MB 넘으면 디스크)에 쌓는다. 즉 업로드 핸들러의 상한
    검사에 도달하는 시점에는 이미 다 받은 뒤라, 길이를 밝히지 않은 요청은 디스크를
    원하는 만큼 채울 수 있었다.

    그래서 본문이 있는데 길이를 밝히지 않은 요청은 411로 거부한다. 브라우저 fetch와
    httpx는 파일·JSON 본문에 항상 Content-Length를 붙이므로 정상 클라이언트는
    영향받지 않는다.
    """

    def __init__(self, app, max_bytes: int):
        super().__init__(app)
        self.max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next) -> Response:
        raw = request.headers.get("content-length")
        if raw is None:
            # 본문이 있다는 신호(chunked)인데 길이가 없으면 여기서 끊는다.
            if "chunked" in request.headers.get("transfer-encoding", "").lower():
                log.warning("길이를 밝히지 않은 본문 거부: %s %s",
                            request.method, request.url.path)
                return _refuse(411, "요청 본문의 길이(Content-Length)를 함께 보내주세요.")
            return await call_next(request)

        try:
            declared = int(raw)
        except ValueError:
            declared = None
        if declared is not None and declared > self.max_bytes:
            log.warning("본문 상한 초과로 거부: %s %s (%d bytes)",
                        request.method, request.url.path, declared)
            return _refuse(413, "요청 본문이 너무 큽니다.")
        return await call_next(request)
