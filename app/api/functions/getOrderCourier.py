"""getOrderCourier — the courier card of one order, for the people on it.
CourierProfile itself is private.

Body: { order_id } (the order's customer, its courier, admins) or { offer_id } (the customer the
offer was sent to, admins). The phone and full name are given for an assigned courier and for
an offer: every assignment and every offer is made by the server now, so both are genuine
(`verified` true). The live position only while the delivery runs. After a block between the
customer and the courier the phone is kept only while the order runs (owner's rule, QA 06/10
B55): no phone once it is over, nor on an offer.
Returns { success, courier, tracking?, verified? }.
"""

from typing import Any

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.functions._common import as_uuid
from app.models import Courier, Order, OrderOffer
from app.security.deps import CurrentUser
from app.services.order_transitions import LIVE_STATUSES
from app.services.safety import is_blocked
from app.services.tracking import courier_card


async def handle(
    payload: dict[str, Any], user: CurrentUser, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    order_id = payload.get("order_id").strip() if isinstance(payload.get("order_id"), str) else ""
    offer_id = payload.get("offer_id").strip() if isinstance(payload.get("offer_id"), str) else ""
    if not order_id and not offer_id:
        return 400, {"error": "order_id or offer_id required"}

    if offer_id:
        oid = as_uuid(offer_id)
        row = (
            (
                await session.execute(
                    select(OrderOffer, Order.customer_id)
                    .join(Order, Order.id == OrderOffer.order_id)
                    .where(OrderOffer.id == oid, OrderOffer.status != "withdrawn")
                )
            ).first()
            if oid
            else None
        )
        if row is None:
            return 404, {"error": "Offer not found"}
        offer, customer_id = row
        if not user.is_admin and customer_id != user.id:
            return 403, {"error": "Forbidden"}
        courier = await session.get(Courier, offer.courier_id)
        if courier is None:
            return 200, {"success": True, "courier": None}
        contact = user.is_admin or not await is_blocked(session, customer_id, courier.user_id)
        card = await courier_card(session, courier, contact=contact, position=False)
        return 200, {"success": True, "courier": card, "verified": True}

    oid = as_uuid(order_id)
    order = await session.get(Order, oid) if oid else None
    if order is None:
        return 404, {"error": "Order not found"}
    is_customer = order.customer_id == user.id
    if order.courier_id is None:
        if not user.is_admin and not is_customer:
            return 403, {"error": "Forbidden"}
        return 200, {"success": True, "courier": None, "tracking": False}
    courier = await session.get(Courier, order.courier_id)
    is_that_courier = courier is not None and courier.user_id == user.id
    if not user.is_admin and not is_customer and not is_that_courier:
        return 403, {"error": "Forbidden"}
    if courier is None:
        return 200, {"success": True, "courier": None, "tracking": False}
    tracking = order.status in LIVE_STATUSES
    contact = user.is_admin or tracking or not await is_blocked(session, order.customer_id, courier.user_id)
    card = await courier_card(session, courier, contact=contact, position=tracking, order_id=order.id)
    return 200, {"success": True, "courier": card, "tracking": tracking, "verified": True}
