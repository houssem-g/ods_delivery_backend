"""updateOrderOffer — the courier changes the price of his pending offer (and, optionally, its
delay and note) while the order is still open for offers.

Body: { offer_id, fee, eta_minutes?, message? } — same bounds as createOrderOffer (0 < fee ≤ 200
TND, 3 decimals; eta 1-600 min, an invalid one keeps the current; message ≤ 300 chars, "" clears).
Returns { success, offer, rank, total, cheapest, tied } (the rank of the new price, getOfferRank).
The customer's OrderOffers screen hears the OrderOffer update event; he is notified in-app
(type new_offer, data.kind = "offer_updated": "Karim a modifié son offre : 6.500 DT"), pushed at
most once per offer every 2 minutes. An unchanged offer is answered without any notice.
Errors: Missing offer_id / invalid_fee (400), courier_profile_missing / courier_not_verified
(403), offer_not_found (404, also someone else's offer), offer_not_pending / order_not_open
(409), too_many_edits (429, 10 changes per offer).
"""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.functions._common import answers_refusals, document
from app.security.deps import CurrentUser
from app.services.offers import update_offer


@answers_refusals
async def handle(
    payload: dict[str, Any], user: CurrentUser, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    offer, rank = await update_offer(session, user, payload)
    return 200, {"success": True, "offer": await document(session, "OrderOffer", user, offer.id), **rank}
