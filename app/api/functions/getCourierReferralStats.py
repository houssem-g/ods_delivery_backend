"""getCourierReferralStats — the calling courier's invite code (created the first time)
and counters: { code, verified, referred_count, ordered_count, delivered_orders,
delivered_by_me }. 404 { error: 'no_courier_profile' } for a non-courier.
"""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ApiError
from app.security.deps import CurrentUser
from app.services.referral import courier_stats_for


async def handle(
    payload: dict[str, Any], user: CurrentUser | None, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    assert user is not None
    try:
        return 200, await courier_stats_for(session, user)
    except ApiError as exc:
        return exc.status, {"error": exc.error}
