"""getOrderReceipt — the receipt photos of an order, as short-lived signed links.

The courier's receipt photo is a private upload; the customer sees it to check the price before
paying cash (owner, 06/10, QA B19). Body { order_id }: the order's customer, its courier, admins.
Returns { success, receipts: [{ seq, shop, amount, url }] } (url valid 10 min).
Errors: order_id required (400), Order not found (404), Forbidden (403).
"""

from typing import Any

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.functions._common import as_uuid
from app.models import Courier, Order, OrderStop
from app.security.deps import CurrentUser
from app.storage import s3

URL_SECONDS = 600


async def handle(
    payload: dict[str, Any], user: CurrentUser, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    order_id = as_uuid(payload.get("order_id"))
    if order_id is None:
        return 400, {"error": "order_id required"}
    order = await session.get(Order, order_id)
    if order is None:
        return 404, {"error": "Order not found"}
    courier_user = (
        (
            await session.execute(select(Courier.user_id).where(Courier.id == order.courier_id))
        ).scalar_one_or_none()
        if order.courier_id
        else None
    )
    if not (user.is_admin or order.customer_id == user.id or courier_user == user.id):
        return 403, {"error": "Forbidden"}
    stops = (
        await session.execute(
            select(OrderStop)
            .where(OrderStop.order_id == order.id, OrderStop.receipt_key.is_not(None))
            .order_by(OrderStop.seq)
        )
    ).scalars()
    receipts = [
        {
            "seq": stop.seq,
            "shop": stop.name,
            "amount": float(stop.purchase_amount) if stop.purchase_amount is not None else None,
            "url": s3.presign_get(stop.receipt_key, URL_SECONDS),
        }
        for stop in stops
    ]
    return 200, {"success": True, "receipts": receipts}
