"""acceptOrderOffer — the customer picks one courier's offer (base44/functions/acceptOrderOffer).

One transaction under the order lock: the offer accepted, the others rejected, the order
assigned with the fee of the offer (never a client value), accepted_at set. Suspended customers
(5 no-response incidents in 180 days) are refused. The courier is notified here (order_accepted).

Body: { order_id, offer_id }. Returns { success, courier_user_id, offer }.
Errors: order_not_found / offer_not_found (404), Forbidden / customer_suspended (403),
order_not_open / offer_not_pending / courier_unavailable (409).
"""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.functions._common import answers_refusals, document
from app.security.deps import CurrentUser
from app.services.offers import accept_offer


@answers_refusals
async def handle(
    payload: dict[str, Any], user: CurrentUser, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    _order, offer, courier_user = await accept_offer(session, user, payload)
    return 200, {
        "success": True,
        "courier_user_id": courier_user.email,
        "offer": await document(session, "OrderOffer", user, offer.id),
    }
