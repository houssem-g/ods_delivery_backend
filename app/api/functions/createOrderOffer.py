"""createOrderOffer — a verified courier sends a price for an open order.
The offer is built from his own profile.

Body: { order_id, fee, eta_minutes?, distance_km?, message?, from_lat?, from_lng? } — `from`: the
courier's position the app used (else the server's recent one); the server computes his arrival
(app/services/arrival.py) and stores it on the offer (estimated_eta_minutes, to_shop_km,
eta_announced, always false now); without eta_minutes (an older app) the computed arrival is the delay; a
delay under the computation, even by one minute, is refused (400 eta_below_computed, min_eta = computed,
computed — owner 19/10/2026, QA M12). The offer's arrival (OrderOffer.arrival_minutes) = his delay if he
raised it above the computation, else the computation.
Returns { success, offer, rank, total, cheapest, tied, eta_rank, my_arrival, board… } (getOfferRank).
Errors: invalid_fee (400), courier_profile_missing / courier_not_verified / own_order (403),
order_not_found (404 — also during another courier's 60 s invite-link head start,
app/services/invite_priority.py), order_not_open / offer_already_sent (409).
The customer is notified here (new_offer); the app's own notification that follows is deduplicated.
"""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.functions._common import answers_refusals, document
from app.security.deps import CurrentUser
from app.services.offers import create_offer, offer_rank


@answers_refusals
async def handle(
    payload: dict[str, Any], user: CurrentUser, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    offer = await create_offer(session, user, payload)
    rank = await offer_rank(
        session,
        offer.order_id,
        offer.courier_id,
        offer.proposed_fee,
        offer.eta_minutes,
        estimated=offer.estimated_eta_minutes,
    )
    return 200, {"success": True, "offer": await document(session, "OrderOffer", user, offer.id), **rank}
