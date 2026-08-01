"""전역 예외 → 공통 응답 포맷(ApiResponse) 변환.

라우터는 그냥 HTTPException을 raise하기만 하면 되고, 형식 맞추는 건 여기서 한다.
응답 본문은 손으로 dict를 짜지 않고 ApiResponse를 그대로 직렬화한다 — 스키마가
바뀌면 성공 응답과 실패 응답이 같이 따라가야 하기 때문이다.
"""
import logging

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded

from app.schemas.common import ApiResponse, ErrorDetail

logger = logging.getLogger(__name__)


def error_response(status_code: int, code: str, message: str,
                   headers: dict | None = None) -> JSONResponse:
    body = ApiResponse[None](
        success=False, data=None, error=ErrorDetail(code=code, message=message))
    return JSONResponse(status_code=status_code, content=body.model_dump(),
                        headers=headers)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(HTTPException)
    async def handle_http_exception(request: Request,
                                    exc: HTTPException) -> JSONResponse:
        return error_response(exc.status_code, str(exc.status_code),
                              str(exc.detail), headers=exc.headers)

    @app.exception_handler(RateLimitExceeded)
    async def handle_rate_limit(request: Request,
                                exc: RateLimitExceeded) -> JSONResponse:
        return error_response(status.HTTP_429_TOO_MANY_REQUESTS, "429",
                              "요청이 너무 많습니다. 잠시 후 다시 시도하세요.")

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request,
                                      exc: RequestValidationError) -> JSONResponse:
        # 상수 이름이 starlette 버전마다 달라(UNPROCESSABLE_ENTITY → _CONTENT)
        # 숫자를 그대로 쓴다.
        return error_response(422, "422", "요청 값이 올바르지 않습니다.")

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request,
                                      exc: Exception) -> JSONResponse:
        logger.exception("Unhandled exception while processing %s %s",
                         request.method, request.url.path)
        return error_response(status.HTTP_500_INTERNAL_SERVER_ERROR, "500",
                              "서버 내부 오류가 발생했습니다.")
