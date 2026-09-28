"""placeOrder — a customer places an order (base44/functions/placeOrder).

Body: { order: { items_text, quantity?, notes?, alternatives?, estimated_price?, package_size?,
  shop_name, shop_address?, shop_phone?, shop_governorate?, shop_city?, shop_lat, shop_lng, shops?,
  delivery_address, delivery_governorate?, delivery_city?, delivery_details?, delivery_lat,
  delivery_lng, preferred_time?, scheduled_time?, customer_phone? } }
Returns { success, order, dispatched }. Errors: invalid_items, invalid_shop, invalid_shop_location,
invalid_delivery_address, invalid_delivery_location, phone_required (400), customer_suspended (403),
too_many_open_orders (429, max). QA orders are not broadcast.
"""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.functions._common import answers_refusals, document
from app.security.deps import CurrentUser
from app.services.dispatch import dispatch_order
from app.services.orders import is_test_order, place_order


@answers_refusals
async def handle(
    payload: dict[str, Any], user: CurrentUser, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    order = await place_order(session, user.id, payload.get("order"))
    dispatched = None
    if not is_test_order(order.items_text):
        dispatched = (await dispatch_order(session, order)).get("dispatched")
    return 200, {
        "success": True,
        "order": await document(session, "Order", user, order.id),
        "dispatched": dispatched,
    }
