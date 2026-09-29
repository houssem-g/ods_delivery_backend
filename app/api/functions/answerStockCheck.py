"""answerStockCheck — the order's customer answers the courier's "article indisponible"
(app/services/stock_checks.py).

Body: { order_id, stock_check_id, decision: 'accept'|'skip'|'cancel' }. accept / skip → the order
goes back to at_shop; cancel → the order is cancelled (reason product_unavailable, no penalty for
anybody). Returns { success, stock_check, order_status }. Errors: 'Missing required fields' /
invalid_decision (400), Unauthorized (403), 'Order not found' / stock_check_not_found (404),
already_decided (409, + stock_check, order_status) / accept_not_allowed (409: no substitute
proposed, or nothing available) / skip_not_allowed (409: nothing available).
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
    return await stock_checks.answer(session, user, payload)
