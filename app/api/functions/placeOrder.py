"""placeOrder — a customer places an order (base44/functions/placeOrder).

Body: { order: { items_text, quantity?, notes?, alternatives?, unavailable_policy? (call_me|substitute|
  skip|cancel, default call_me), estimated_price?, package_size?,
  shop_name, shop_address?, shop_phone?, shop_governorate?, shop_city?, shop_lat, shop_lng, shops?,
  delivery_address, delivery_governorate?, delivery_city?, delivery_details?, delivery_lat,
  delivery_lng, preferred_time?, scheduled_time?, customer_phone? }, draft_id? }
(draft_id: the caller's order draft, deleted with the placement.)
Returns { success, order, dispatched }. Errors: invalid_items, invalid_shop, invalid_shop_location,
invalid_delivery_address, invalid_delivery_location, phone_required, phone_unverified (400: the
profile's foreign number was not confirmed by the WhatsApp code, see requestPhoneVerification),
customer_suspended (403), too_many_open_orders (429, max). QA orders are not broadcast.
"""

import logging
from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.functions._common import answers_refusals, document
from app.security.deps import CurrentUser
from app.services import order_drafts
from app.services.dispatch import dispatch_order
from app.services.orders import is_test_order, place_order

log = logging.getLogger("odsd.functions")


@answers_refusals
async def handle(
    payload: dict[str, Any], user: CurrentUser, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    order = await place_order(session, user.id, payload.get("order"))
    # The draft this order came from goes with it (same transaction: kept if the order fails).
    await order_drafts.remove(session, user.id, payload.get("draft_id"))
    dispatched = None
    if not is_test_order(order.items_text):
        try:  # the order exists: a failed broadcast never fails it (couriers also list open orders)
            async with session.begin_nested():
                dispatched = (await dispatch_order(session, order)).get("dispatched")
        except Exception:
            log.exception("placeOrder: dispatch failed for %s", order.id)
    return 200, {
        "success": True,
        "order": await document(session, "Order", user, order.id),
        "dispatched": dispatched,
    }
