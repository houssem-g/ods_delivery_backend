"""listHotDeals — the hot deals a customer may reserve, nearest first
(base44/functions/listHotDeals; app/services/hot_deals.py).

Body: { lat?, lng?, radius_km = 50 (≤ 200), limit = 30 (1-50), cursor?, id? } — `id`: that deal only
(the detail page; [] once it is reserved, expired or unknown).
Returns { success, deals: [public fields + distance_km (0.1 km, null without a point)], total,
next_cursor }. Public fields (Aurora): discounted_price = current_price (decayed now), start_price,
floor_price, next_price / next_drop_at (null at the floor), original_price (= purchase_amount),
discount_pct_now, purchased_at, no_response_at, listed_at, courier_name (first name + initial),
courier_rating, courier_deliveries, receipt_verified, sealed (always false: the front decides).
The courier's phone and position and the buyer never leave the server here.
"""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.security.deps import CurrentUser
from app.services import hot_deals


async def handle(
    payload: dict[str, Any], user: CurrentUser, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    return await hot_deals.list_deals(session, payload)
