"""cancelOrder and getCancellationPolicy (base44/functions/cancelOrder, getCancellationPolicy).

Rules kept from the live function:
- customer: only his order, only while pending / offers_received / accepted;
- courier: only his own profile id and his own order, only while the delivery runs
  (COURIER_CANCELLABLE; a delivered or cancelled order is never sent back to the pool);
  a late drop (at_shop … client_no_response) counts a late cancellation, except a
  verified "client ne répond pas" (procedure run to its end, deadline past): then the
  customer gets the incident and the order is closed (not re-dispatched);
- a courier leaving a regular order puts it back to 'pending' in one write, clears
  the courier, his fee and live position, expires his offers and re-dispatches it;
  a hot-deal order is cancelled instead and its deal expires;
- a customer cancellation rejects the pending offers, relists a reserved hot deal
  while it is not expired, and tells the courier.
Notifications are always pushed (no preference gate), as live.
"""

import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Courier, HotDeal, NoResponseCase, Order, OrderOffer, OrderStop
from app.realtime.events import emit
from app.security.deps import CurrentUser
from app.services import order_texts
from app.services import order_transitions as ot
from app.services.dispatch import dispatch_order
from app.services.offers import close_pending_offers
from app.services.order_notices import notify_always_pushed
from app.services.orders import OrderRefused, courier_of_user, first_stop, is_test_order

log = logging.getLogger("odsd.cancellation")
CUSTOMER_CANCELLABLE = ("pending", "offers_received", "accepted")
COURIER_CANCELLABLE = (
    "accepted",
    "at_shop",
    "price_confirmation_needed",
    "purchased",
    "on_the_way",
    "client_no_response",
)
PENALTY_STATUSES = ("at_shop", "purchased", "on_the_way", "client_no_response")
NO_RESPONSE_REASONS = frozenset({"client_no_response", "cannot_reach_customer", "goods_returned_to_shop"})
LEGACY_WAIT = timedelta(minutes=2)

# Filled by the no-response feature: brings the case up to date before a no-response
# cancellation is judged (the live code calls triggerEmergencyContact {action:'status'}).
NoResponseRefresh = Callable[[AsyncSession, Order], Awaitable[None]]
no_response_refresh: NoResponseRefresh | None = None


async def latest_case(session: AsyncSession, order_id: uuid.UUID) -> NoResponseCase | None:
    return (
        await session.execute(
            select(NoResponseCase)
            .where(NoResponseCase.order_id == order_id)
            .order_by(NoResponseCase.created_at.desc())
            .limit(1)
            .with_for_update()
        )
    ).scalar_one_or_none()


def no_response_gate(order_status: str, case: NoResponseCase | None, now: datetime) -> dict[str, Any]:
    """Same rule as createHotDeal.noResponseGate: deadline past, customer silent."""
    if case is None:
        return {"ok": False, "reason": "no_report"}
    customer_answered = case.resolution == "customer_confirmed"
    if customer_answered and order_status != "client_no_response":
        return {"ok": False, "reason": "customer_answered"}
    if case.status == "resolved":
        answered = case.resolution in ("customer_confirmed", "courier_reached")
        return {"ok": False, "reason": "customer_answered" if answered else "already_closed"}
    deadline = case.deadline_at or (case.started_at + LEGACY_WAIT)
    if now < deadline:
        return {"ok": False, "reason": "wait", "deadline_at": deadline.isoformat()}
    return {"ok": True}


async def linked_hot_deal(session: AsyncSession, order: Order) -> HotDeal | None:
    conditions = [HotDeal.original_order_id == order.id, HotDeal.buyer_order_id == order.id]
    if order.resale_deal_id is not None:
        conditions.append(HotDeal.id == order.resale_deal_id)
    rows = (
        await session.execute(
            select(HotDeal).where(or_(*conditions)).order_by(HotDeal.created_at.desc()).with_for_update()
        )
    ).scalars()
    deals = list(rows)
    # the deal this order was bought from wins (Order.resale_order_id), then the one made from it
    for deal in deals:
        if deal.id == order.resale_deal_id or deal.buyer_order_id == order.id:
            return deal
    return deals[0] if deals else None


async def _reset_stops(session: AsyncSession, order: Order) -> None:
    """Back to the pool: the next courier starts from the first shop again."""
    stops = (await session.execute(select(OrderStop).where(OrderStop.order_id == order.id))).scalars()
    for stop in stops:
        stop.status, stop.purchase_amount, stop.receipt_key, stop.completed_at = "pending", None, None, None
    order.purchase_amount = None
    order.current_stop_seq = 0


async def cancel_order(session: AsyncSession, user: CurrentUser, payload: dict[str, Any]) -> None:
    order_id, cancelled_by, courier_id = (
        payload.get("order_id"),
        payload.get("cancelled_by"),
        payload.get("courier_id"),
    )
    reason = payload.get("reason").strip()[:200] if isinstance(payload.get("reason"), str) else ""
    if not order_id or not reason or not cancelled_by:
        raise OrderRefused(400, "Missing required fields")
    if cancelled_by not in ("customer", "courier"):
        raise OrderRefused(400, "Invalid cancelled_by")
    order = await ot.lock_order(session, str(order_id))
    if order is None:
        raise OrderRefused(404, "Order not found")
    now = ot.now_utc()
    case: NoResponseCase | None = None
    verified_no_response = False

    if cancelled_by == "customer":
        if order.customer_id != user.id:
            raise OrderRefused(403, "Unauthorized")
        if order.status not in CUSTOMER_CANCELLABLE:
            raise OrderRefused(400, "Cannot cancel order at this stage", can_cancel=False)
    else:
        mine = await courier_of_user(session, user.id, lock=True)
        if mine is None or str(mine.id) != str(courier_id) or order.courier_id != mine.id:
            raise OrderRefused(403, "Unauthorized")
        if order.status not in COURIER_CANCELLABLE:
            raise OrderRefused(400, "Cannot cancel order at this stage", can_cancel=False)
        if reason in NO_RESPONSE_REASONS:
            if no_response_refresh is not None:
                await no_response_refresh(session, order)
            case = await latest_case(session, order.id)
            verified_no_response = no_response_gate(order.status, case, now)["ok"]
        if order.status in PENALTY_STATUSES and not verified_no_response:
            mine.late_cancellations += 1
            emit(session, "CourierProfile", "update", mine.id)
        if case is None:
            case = await latest_case(session, order.id)

    if case is not None and case.status != "resolved" and cancelled_by == "courier":
        case.status, case.resolved_at = "resolved", now
        if verified_no_response:
            case.resolution = "returned_to_shop" if reason == "goods_returned_to_shop" else "cancelled_kept"
            case.incident_counted = True
            case.final_at = case.final_at or now
        else:
            case.resolution, case.incident_counted = "courier_cancelled_other", False

    previous_courier_id = order.courier_id
    deal = await linked_hot_deal(session, order)
    order.cancel_reason, order.cancelled_by = reason, cancelled_by
    back_to_pool = cancelled_by == "courier" and deal is None and not verified_no_response
    if back_to_pool:
        await ot.transition(session, order, "pending", user, "cancelOrder", reason, cancelled_by="courier")
        order.cancelled_at = now
        order.courier_id = None
        order.delivery_fee = None
        await _reset_stops(session, order)
        await session.flush()
    else:
        # A customer cancellation keeps the courier on the order: he can still open it and see
        # that it was cancelled (Base44 kept courier_user_id for that).
        await ot.transition(
            session, order, "cancelled", user, "cancelOrder", reason, cancelled_by=cancelled_by
        )

    stop = await first_stop(session, order.id)
    shop_name = stop.name if stop else None
    if cancelled_by == "customer":
        await close_pending_offers(session, OrderOffer.order_id == order.id, "rejected")
        if deal is not None:
            deal.status = "expired" if deal.expires_at <= now else "available"
            deal.buyer_id, deal.reserved_at, deal.buyer_order_id = None, None, None
            emit(session, "ResaleOrder", "update", deal.id)
        if previous_courier_id is not None:
            courier = await session.get(Courier, previous_courier_id)
            if courier is not None:
                await notify_always_pushed(
                    session,
                    user_id=courier.user_id,
                    type_="order_cancelled",
                    order_id=order.id,
                    metadata={"reason": reason, "cancelled_by": cancelled_by, "recipient_role": "courier"},
                    **order_texts.cancelled_by_customer(shop_name, reason),
                )
        return

    await notify_always_pushed(
        session,
        user_id=order.customer_id,
        type_="order_cancelled",
        order_id=order.id,
        metadata={"reason": reason, "cancelled_by": cancelled_by, "recipient_role": "customer"},
        **order_texts.cancelled_by_courier(reason, verified_no_response, deal is not None),
    )
    if deal is not None and not verified_no_response:
        deal.status = "expired"
        emit(session, "ResaleOrder", "update", deal.id)
    # every offer of this courier on the order (his accepted one included)
    await session.execute(
        OrderOffer.__table__.update()
        .where(
            OrderOffer.order_id == order.id,
            OrderOffer.courier_id == previous_courier_id,
            OrderOffer.status.in_(("pending", "accepted")),
        )
        .values(status="expired", decided_at=now)
    )
    offer_ids = (
        await session.execute(
            select(OrderOffer.id).where(
                OrderOffer.order_id == order.id, OrderOffer.courier_id == previous_courier_id
            )
        )
    ).scalars()
    for offer_id in offer_ids:
        emit(session, "OrderOffer", "update", offer_id)
    if back_to_pool and not is_test_order(order.items_text):
        try:  # the cancellation is done: a failed re-broadcast must not undo it
            async with session.begin_nested():
                await dispatch_order(session, order)
        except Exception:
            log.exception("cancelOrder: re-dispatch failed for %s", order.id)


# --- getCancellationPolicy ---------------------------------------------------------------------------

CUSTOMER_DELAY_CANCEL_STATUSES = ("purchased", "at_shop")


def customer_policy(status: str) -> dict[str, Any]:
    if status in CUSTOMER_CANCELLABLE:
        return {
            "can_cancel": True,
            "is_delay_cancel": False,
            "penalty_applies": False,
            "reason_code": "free_cancel",
            "message_ar": "يمكنك الإلغاء بدون رسوم",
            "message_fr": "Vous pouvez annuler sans frais",
        }
    if status in CUSTOMER_DELAY_CANCEL_STATUSES:
        return {
            "can_cancel": True,
            "is_delay_cancel": True,
            "penalty_applies": True,
            "reason_code": "post_purchase_cancel",
            "message_ar": "الإلغاء ممكن لكن قد تنطبق رسوم بعد الشراء",
            "message_fr": "Annulation possible mais des frais peuvent s'appliquer après achat",
        }
    return {
        "can_cancel": False,
        "is_delay_cancel": False,
        "penalty_applies": False,
        "reason_code": "locked_by_status",
        "message_ar": "لا يمكن الإلغاء في هذه المرحلة",
        "message_fr": "Annulation impossible à cette étape",
    }


def courier_policy(status: str) -> dict[str, Any]:
    penalty = status in PENALTY_STATUSES
    return {
        "can_cancel": True,
        "is_delay_cancel": penalty,
        "penalty_applies": penalty,
        "reason_code": "courier_late_cancel_penalty" if penalty else "courier_cancel_ok",
        "message_ar": "يمكنك الإلغاء لكن قد يؤثر ذلك على تقييمك" if penalty else "يمكنك إلغاء الطلب",
        "message_fr": "Annulation possible mais cela peut impacter votre note"
        if penalty
        else "Vous pouvez annuler la commande",
    }
