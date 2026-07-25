import logging

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


def _error_body(code: str, message: str) -> dict:
    return {"success": False, "data": None, "error": {"code": code, "message": message}}


def register_exception_handlers(app: FastAPI) -> None:
    """
    앱 전체에서 발생하는 예외를 잡아서 공통 응답 포맷(ApiResponse)으로 변환합니다.
    라우터에서는 그냥 HTTPException을 raise하기만 하면 되고, 형식 맞추는 건 여기서 처리합니다.
    """

    @app.exception_handler(HTTPException)
    async def handle_http_exception(request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(str(exc.status_code), str(exc.detail)),
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=_error_body("422", "요청 값이 올바르지 않습니다."),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled exception while processing %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_error_body("500", "서버 내부 오류가 발생했습니다."),
        )
