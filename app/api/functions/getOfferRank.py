"""getOfferRank — where a courier's price stands among the other couriers' pending offers on
an open order ("1ʳᵉ sur 3 offres — la moins chère"), live while he edits it.

Body: { order_id, fee? } — `fee`: the price he is typing; omitted, his pending offer's price.
Returns { success, rank, total, cheapest, tied, other_fees: [..ascending], lowest_other, my_offer: {id, fee} | null }:
rank = 1 + other pending offers strictly cheaper, total = other pending offers + 1,
tied = others at the same price (they share the rank).
Body { order_ids: [...] (≤ 20) } instead: his pending offers on those open orders in one call
(the "Mes offres" list) → { success, ranks: { <order_id>: {rank, total, cheapest, tied, my_offer} } }.
The other couriers' prices come back anonymous (other_fees): never an id or a name.
Errors: invalid_fee / invalid_order_ids (400), courier_profile_missing / courier_not_verified /
own_order (403), order_not_found / offer_not_found (404, no fee and no pending offer),
order_not_open (409), too_many_rank_requests (429, RATE_LIMIT_OFFER_RANK per courier: probing
the others' prices one fee after another costs calls).
"""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.functions._common import answers_refusals
from app.config import settings
from app.rate_limit import allow
from app.security.deps import CurrentUser
from app.services.offers import get_rank


@answers_refusals
async def handle(
    payload: dict[str, Any], user: CurrentUser, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    if not allow("getOfferRank", f"user:{user.id}", settings.RATE_LIMIT_OFFER_RANK):
        # own code, not the generic limiter's text: the front pauses all its polls on that one
        return 429, {"error": "too_many_rank_requests", "limit": settings.RATE_LIMIT_OFFER_RANK}
    return 200, await get_rank(session, user, payload)
