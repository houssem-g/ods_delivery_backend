"""GET /api/public/vitrine/merchant/{token} — the shop owner's private page (Vitrine, contract §8).

No account: the 32-byte token of the link IS the access (found by sha256; « Nouveau lien » in the
admin kills the old one). 60 reads per hour per token and per address. Never indexed. The answer
holds counters only: no customer name, phone or address.
"""

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from slowapi.util import get_remote_address
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.errors import ApiError
from app.rate_limit import allow
from app.services import vitrine_stats
from app.services.vitrine import token_hash

router = APIRouter(prefix="/api/public/vitrine", tags=["vitrine"])
RATE = "60/hour"
NO_INDEX = {"X-Robots-Tag": "noindex, nofollow", "Cache-Control": "no-store"}


@router.get("/merchant/{token}")
async def merchant(
    token: str,
    request: Request,
    month: str | None = Query(default=None, max_length=7),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    key = token_hash(token)[:32]
    if not allow("vitrine_merchant", key, RATE) or not allow(
        "vitrine_merchant_ip", get_remote_address(request), RATE
    ):
        raise ApiError(429, "rate_limited", "Rate limit exceeded (too many requests)")
    body: dict[str, Any] = await vitrine_stats.merchant_stats(session, token, month)
    await session.commit()  # the « ouvert N fois » counter
    return JSONResponse(body, headers=NO_INDEX)
