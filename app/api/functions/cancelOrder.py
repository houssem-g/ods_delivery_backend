"""cancelOrder — the customer or the assigned courier cancels (base44/functions/cancelOrder;
rules in app/services/cancellation.py).

Body: { order_id, reason, cancelled_by: 'customer'|'courier', courier_id? }.
Returns { success, message }. Errors: 'Missing required fields' / 'Invalid cancelled_by' /
'Cannot cancel order at this stage' (+ can_cancel false) (400), 'Unauthorized' (403),
'Order not found' (404).
"""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.functions._common import answers_refusals
from app.security.deps import CurrentUser
from app.services.cancellation import cancel_order


@answers_refusals
async def handle(
    payload: dict[str, Any], user: CurrentUser, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    await cancel_order(session, user, payload)
    return 200, {"success": True, "message": "Order cancelled successfully"}
