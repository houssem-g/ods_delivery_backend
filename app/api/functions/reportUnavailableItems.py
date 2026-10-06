"""reportUnavailableItems — the assigned courier, at the shop before buying, reports items the
customer asked for that the shop does not have (app/services/stock_checks.py).

Body: { order_id, missing_text, substitute_text?, substitute_price? (TND, 0..2000), photo_url?
(one of the courier's public uploads), nothing_available?: bool, courier_id?, missing_price? (TND,
0..2000: the missing item's price), quantity? (1..100, default 1: how many are missing) }.
Returns { success, stock_check, order_status }. Errors: 'Missing required fields' /
missing_text_required / substitute_text_required / invalid_price / invalid_missing_price (+ max) /
invalid_quantity (+ max) / invalid_photo (400),
Unauthorized (403), 'Order not found' (404), not_reportable (409, + status) /
stock_check_pending (409, + stock_check), item_already_reported (409, + stock_check: the same
article — or « rien n'est disponible » — was already reported on this order), too_many_stock_checks
(429, + max).
"""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.functions._common import answers_refusals
from app.security.deps import CurrentUser
from app.services import stock_checks


@answers_refusals
async def handle(
    payload: dict[str, Any], user: CurrentUser, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    return await stock_checks.report(session, user, payload)
