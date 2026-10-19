"""getCancellationPolicy — what cancelling would mean now, for the order's customer or courier.
Parties and admins only.

Body: { order_id, actor?: 'customer'|'courier' }. Returns { success, actor, order_id, status, policy }.
Courier after the purchase (policy.after_purchase): policy.can_resell says whether createHotDeal would
take the goods now; false → resale_refusal (wait | customer_answered | already_closed | nothing_bought |
order_not_resellable), resale_after (ISO, when `wait`), and the message names only what is possible
(finish the delivery, or « rendre au magasin », a late cancellation) — QA 19/10 M6.
"""

from typing import Any

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.functions._common import as_uuid
from app.models import Order
from app.security.deps import CurrentUser
from app.services import order_transitions as ot
from app.services.cancellation import (
    after_purchase,
    courier_policy,
    customer_policy,
    late_for_courier,
    resale_possible,
)
from app.services.orders import is_assigned_courier


async def handle(
    payload: dict[str, Any], user: CurrentUser, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    order_id = payload.get("order_id")
    if not order_id:
        return 400, {"error": "Missing required field: order_id"}
    actor = payload.get("actor") or "customer"
    oid = as_uuid(order_id)
    order = (
        (await session.execute(select(Order).where(Order.id == oid))).scalar_one_or_none() if oid else None
    )
    if order is None:
        return 404, {"error": "Order not found"}
    if not (
        user.is_admin or order.customer_id == user.id or await is_assigned_courier(session, order, user.id)
    ):
        return 403, {"error": "Forbidden"}
    if actor == "courier":
        resale = await resale_possible(session, order, ot.now_utc()) if after_purchase(order) else None
        policy = courier_policy(order, late=await late_for_courier(session, order), resale=resale)
    else:
        policy = customer_policy(order.status)
    return 200, {
        "success": True,
        "actor": actor,
        "order_id": order_id,
        "status": order.status,
        "policy": policy,
    }
