"""releaseBlockedCourier — the customer blocked the courier of his order and chooses another one.

Body { order_id }. Before the purchase only (accepted / at_shop / price_confirmation_needed): the
courier leaves the order without penalty, it goes back to 'pending' and is offered again (never to
the blocked courier). Returns { success, status }. Errors: order_not_found (404), Unauthorized
(403), no_courier / already_purchased (+ status) / not_blocked (409). Logic: app/services/cancellation.py.
"""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.functions._common import answers_refusals
from app.security.deps import CurrentUser
from app.services.cancellation import release_blocked_courier


@answers_refusals
async def handle(
    payload: dict[str, Any], user: CurrentUser, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    return 200, await release_blocked_courier(session, user, payload)
