"""reserveHotDeal — a customer reserves a hot deal (base44/functions/reserveHotDeal; rules in
app/services/hot_deals.py).

Body: { resale_order_id, delivery_address, delivery_lat?, delivery_lng?, phone? }.
Returns { success, order_id, resale_order_id, courier_phone, price (the decayed price charged at
this instant: the order's purchase_amount) }. Errors: 'Missing required
fields' / phone_unverified (400: a foreign number not confirmed by the WhatsApp code),
'You cannot reserve your own hot deal' (403), 'Hot deal not found' (404),
'Hot deal is no longer available' / 'Hot deal has expired' (409), too_many_reservations (429).
"""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.security.deps import CurrentUser
from app.services import hot_deals, no_response


async def handle(
    payload: dict[str, Any], user: CurrentUser, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    status, body = await hot_deals.reserve_deal(session, user, payload)
    if status >= 400 and session.info.pop(no_response.COMMIT_REFUSAL, False):
        await session.commit()
    return status, body
