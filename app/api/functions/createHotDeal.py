"""createHotDeal — the courier resells the goods of an order whose customer stopped answering
(rules in app/services/hot_deals.py).

Body: { order_id, discount_percentage? (0-90, clamped), delivery_fee?,
photo_url?, lang? }. The price is fixed for the deal's whole life (owner 19/10/2026: no automatic drop,
no floor; a `floor_price` sent by an older app is ignored).
Returns { success, deal_id, price, price_fixed: true, start_price (= price), floor_price / drop_step /
drop_every_min / floor_after_min / price_schedule (null, kept for the installed apps), expires_in_min
(120), expires_at, alerted (opted-in customers told) }. `preview: true` (QA 19/10 M8): the same checks
and numbers, nothing created — { success, preview: true, price, … } for the sheet before « Publier ».
Errors: 'Missing order_id' (400), 'Only the courier of this order
can resell it' (403), 'Order not found' (404), order_not_resellable / wait (+ deadline_at) /
customer_answered / already_closed / no_report / already_listed (+ deal_id) (409).
"""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.security.deps import CurrentUser
from app.services import hot_deals, no_response


async def handle(
    payload: dict[str, Any], user: CurrentUser, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    status, body = await hot_deals.create_deal(session, user, payload)
    if status >= 400 and session.info.pop(no_response.COMMIT_REFUSAL, False):
        await session.commit()
    return status, body
