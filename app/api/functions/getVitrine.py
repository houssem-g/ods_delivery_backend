"""getVitrine — client, account optional: { slug | shop_id, src?, device_id? } → { success, vitrine }
(the page /v/<slug>: catalog, hours, ordering state). Counts the visit and the poster scan (once per
device and day). Hidden or unknown → 404 not_found; paused / archived → 404 vitrine_unavailable.
Without an account: 120 reads per hour per address.
Vitrine, contract: ods-audit/livraison-1610/vitrine/CONTRAT-A.md §4."""

from typing import Any

from fastapi import Request
from slowapi.util import get_remote_address
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ApiError
from app.rate_limit import allow
from app.security.deps import CurrentUser
from app.services import vitrine

AUTH = "optional"
ANONYMOUS_RATE = "120/hour"


async def handle(
    payload: dict[str, Any], user: CurrentUser | None, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    address = get_remote_address(request)
    if user is None and not allow("vitrine_read", address, ANONYMOUS_RATE):
        raise ApiError(429, "rate_limited", "Rate limit exceeded (too many requests)")
    client_key = f"{address}|{request.headers.get('user-agent', '')[:200]}"
    return 200, await vitrine.get_vitrine(session, user, payload, client_key)
