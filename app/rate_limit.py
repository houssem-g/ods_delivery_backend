"""slowapi limiter: per signed-in user when a valid bearer token is present, else per client IP."""

from fastapi import Request
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.config import settings
from app.security.tokens import InvalidToken, decode_access_token


def _rate_key(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        try:
            return f"user:{decode_access_token(auth[7:].strip())['sub']}"
        except InvalidToken:
            pass
    return f"ip:{get_remote_address(request)}"


limiter = Limiter(
    key_func=_rate_key,
    default_limits=[settings.RATE_LIMIT_DEFAULT],
    enabled=settings.RATE_LIMIT_ENABLED,
    headers_enabled=False,
)


def ip_key(request: Request) -> str:
    """Auth endpoints are limited per IP: the caller is not signed in yet."""
    return f"ip:{get_remote_address(request)}"


async def rate_limit_exceeded(_: Request, exc: RateLimitExceeded) -> JSONResponse:
    # "Rate limit exceeded" is the text the front's rateGate matches (Base44 wording).
    return JSONResponse(
        {
            "error": "rate_limited",
            "message": "Rate limit exceeded (too many requests)",
            "limit": str(exc.detail),
        },
        status_code=429,
    )
