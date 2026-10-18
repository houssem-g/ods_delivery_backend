"""placeOrder — a customer places an order.

Body: { order: { items_text, quantity?, notes?, alternatives?, unavailable_policy? (call_me|substitute|
  skip|cancel, default call_me), estimated_price?, package_size?,
  shop_name, shop_address?, shop_phone?, shop_governorate?, shop_city?, shop_lat, shop_lng, shops?,
  delivery_address, delivery_governorate?, delivery_city?, delivery_details?, delivery_lat,
  delivery_lng, preferred_time?, scheduled_time?, customer_phone?, budget_max? (0-2000, the
  ceiling for the purchase), preferred_courier_id? (a courier who delivered one of the caller's
  orders: "même livreur"; anything else is ignored and the referral attribution applies) },
  draft_id? }
(draft_id: the caller's order draft, deleted with the placement.)
Returns { success, order, dispatched }. Errors: invalid_items, invalid_shop, invalid_shop_location,
invalid_delivery_address, invalid_delivery_location, invalid_budget (+ max), phone_required,
phone_unverified (400: the profile's foreign number was not confirmed by the WhatsApp code, see
requestPhoneVerification),
too_many_open_orders (429, max). QA orders are not broadcast.
« Je viens chercher » (order.vitrine.fulfillment = pickup, client_ref = the cart's attempt key): the
restaurant's ODS takes it inside this request (app/services/vitrine_sync.py), else 503
restaurant_unreachable and nothing is kept; the same client_ref again answers the same order.
"""

import logging
from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.functions._common import answers_refusals, document
from app.security.deps import CurrentUser
from app.services import order_drafts, vitrine_sync
from app.services.dispatch import dispatch_order
from app.services.orders import place_order

log = logging.getLogger("odsd.functions")


@answers_refusals
async def handle(
    payload: dict[str, Any], user: CurrentUser, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    try:
        order = await place_order(session, user.id, payload.get("order"))
    except vitrine_sync.ConfirmFailed as exc:
        # « Je viens chercher »: ODS did not take it → nothing kept; the customer retries (same ref)
        await vitrine_sync.on_failure(session, exc)
        return exc.status, exc.body()
    if (
        order.fulfillment == "pickup"
    ):  # no courier at all; ODS already has it (vitrine_sync), or it is a retry
        if not session.info.pop("vitrine_existing", False):
            await order_drafts.remove(session, user.id, payload.get("draft_id"))
            await vitrine_sync.commit_or_compensate(session)
        return 200, {
            "success": True,
            "order": await document(session, "Order", user, order.id),
            "dispatched": 0,
        }
    # The draft this order came from goes with it (same transaction: kept if the order fails).
    await order_drafts.remove(session, user.id, payload.get("draft_id"))
    dispatched = None
    try:  # the order exists: a failed broadcast never fails it (couriers also list open orders)
        # (a QA order reaches the QA couriers only: app/services/dispatch.py)
        async with session.begin_nested():
            dispatched = (await dispatch_order(session, order)).get("dispatched")
    except Exception:
        log.exception("placeOrder: dispatch failed for %s", order.id)
    return 200, {
        "success": True,
        "order": await document(session, "Order", user, order.id),
        "dispatched": dispatched,
    }
