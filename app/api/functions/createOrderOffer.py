"""createOrderOffer — a verified courier sends a price for an open order
(base44/functions/createOrderOffer). The offer is built from his own profile.

Body: { order_id, fee, eta_minutes?, distance_km?, message? }. Returns { success, offer }.
Errors: invalid_fee (400), courier_profile_missing / courier_not_verified / own_order (403),
order_not_found (404), order_not_open / offer_already_sent (409).
The customer is notified here (new_offer); the app's own notification that follows is deduplicated.
"""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.functions._common import answers_refusals, document
from app.security.deps import CurrentUser
from app.services.offers import create_offer


@answers_refusals
async def handle(
    payload: dict[str, Any], user: CurrentUser, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    offer = await create_offer(session, user, payload)
    return 200, {"success": True, "offer": await document(session, "OrderOffer", user, offer.id)}
