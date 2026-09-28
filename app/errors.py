"""Error shape `{error, message}` (ARCHITECTURE §6.3) for every failure the API returns."""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import ClientDisconnect

_DEFAULT_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    410: "gone",
    413: "payload_too_large",
    415: "unsupported_media_type",
    429: "rate_limited",
    503: "unavailable",
}


class ApiError(Exception):
    def __init__(self, status: int, error: str, message: str | None = None, **extra: Any) -> None:
        super().__init__(message or error)
        self.status = status
        self.error = error
        self.message = message or error
        self.extra = extra

    def body(self) -> dict[str, Any]:
        return {"error": self.error, "message": self.message, **self.extra}


def error_response(status: int, error: str, message: str, **extra: Any) -> JSONResponse:
    return JSONResponse({"error": error, "message": message, **extra}, status_code=status)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(exc.body(), status_code=exc.status)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _DEFAULT_CODES.get(exc.status_code, "error")
        message = exc.detail if isinstance(exc.detail, str) else code
        return JSONResponse(
            {"error": code, "message": message}, status_code=exc.status_code, headers=exc.headers
        )

    @app.exception_handler(ClientDisconnect)
    async def _client_gone(_: Request, __: ClientDisconnect) -> Response:
        # The browser left (navigation, closed tab) while its body was being read: nothing
        # to answer and nothing wrong server side (it was a 500 with an ERROR traceback).
        return Response(status_code=499)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        details = [
            {"field": ".".join(str(p) for p in err.get("loc", ())[1:]), "message": err.get("msg", "")}
            for err in exc.errors()
        ]
        return error_response(400, "validation_error", "Invalid request", details=details)
