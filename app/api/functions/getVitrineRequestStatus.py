"""getVitrineRequestStatus — no account: { token } (the follow token answered by submitVitrineRequest)
→ { success, request: { id, status, shop_name, created_at, decided_at, refuse_reason, shop } };
404 not_found. 60 reads per hour per address. Rules: app/services/vitrine_requests.py."""

from typing import Any

from fastapi import Request
from slowapi.util import get_remote_address
from sqlalchemy.ext.asyncio import AsyncSession

from app.security.deps import CurrentUser
from app.services import vitrine_requests

AUTH = "optional"


async def handle(
    payload: dict[str, Any], user: CurrentUser | None, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    return 200, await vitrine_requests.status_by_token(session, payload, get_remote_address(request))
