"""signalOfferIntent — a verified courier has (or no longer has) the offer sheet of an open order
open: the customer's offers page shows "un livreur prépare une offre…" (Order.preparing_offers,
app/services/offer_intents.py).

Body: { order_id, active: bool } — true while the sheet is open (send it again at most every
minute or so to keep it alive: it counts for 3 minutes), false when it closes. Sending the offer
also ends it. Returns { success, active, ttl_seconds? }.
Errors: 'Missing order_id' / invalid_active (400), courier_profile_missing / courier_not_verified /
own_order (403), order_not_found (404), order_not_open (+ status) / order_dropped (409),
too_many_intent_signals (429, 30 per minute per courier).
"""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.functions._common import answers_refusals
from app.rate_limit import allow
from app.security.deps import CurrentUser
from app.services import offer_intents

RATE = "30/minute"


@answers_refusals
async def handle(
    payload: dict[str, Any], user: CurrentUser, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    if not allow("signalOfferIntent", f"user:{user.id}", RATE):
        return 429, {"error": "too_many_intent_signals", "limit": RATE}
    return 200, await offer_intents.signal(session, user, payload)
