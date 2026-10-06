"""cancelOrder and getCancellationPolicy (base44/functions/cancelOrder, getCancellationPolicy).

Rules kept from the live function:
- customer: only his order, only while pending / offers_received / accepted;
- courier: only his own profile id and his own order, only while the delivery runs
  (COURIER_CANCELLABLE; a delivered or cancelled order is never sent back to the pool);
  a late drop (at_shop … client_no_response) counts a late cancellation, except a
  verified "client ne répond pas" (procedure run to its end, deadline past): then the
  customer gets the incident and the order is closed (not re-dispatched);
- a courier's product_unavailable cancellation after an unanswered "article indisponible"
  (app/services/stock_checks.py: his latest check on the order expired, the order still waits)
  is fault-free: no late cancellation, the order is cancelled (the shop does not have it, no
  re-dispatch) and the customer is told it costs him nothing; any other cancellation closes a
  pending stock check with the order;
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
from app.services import order_texts, stock_checks
from app.services import order_transitions as ot
from app.services.dispatch import dispatch_order
from app.services.offers import close_pending_offers
from app.services.order_notices import notify_always_pushed
from app.services.orders import OrderRefused, courier_of_user, first_stop, is_test_order, mirror_incidents
from app.services.safety import is_blocked

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
# Owner's rules (06/10, QA B26/B31):
# - after the purchase the courier can't just cancel (the order would go to a 2nd courier who buys
#   the same items again): he resells (createHotDeal courier_choice) or returns the goods to the shop;
# - « Magasin fermé » before the purchase: no penalty, the order is not sent to other couriers.
AFTER_PURCHASE = ("purchased", "on_the_way")
RETURNED_TO_SHOP = "returned_to_shop"
SHOP_CLOSED = "shop_closed"
BEFORE_PURCHASE = ("accepted", "at_shop", "price_confirmation_needed")
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
    verified_stock_check = False

    if cancelled_by == "customer":
        if order.customer_id != user.id:
            raise OrderRefused(403, "Unauthorized")
        if order.status not in CUSTOMER_CANCELLABLE:
            raise OrderRefused(400, "Cannot cancel order at this stage", can_cancel=False)
    else:
        mine = await courier_of_user(session, user.id, lock=True)
        if mine is None or str(mine.id) != str(courier_id) or order.courier_id != mine.id:
            raise OrderRefused(403, "Unauthorized")
        if reason == stock_checks.REASON and order.status in COURIER_CANCELLABLE:
            verified_stock_check = await stock_checks.courier_may_cancel_free(session, order, mine.id)
            if order.status == "cancelled":
                return  # the stock check's own policy (cancel) closed it a moment ago
        if order.status not in COURIER_CANCELLABLE:
            raise OrderRefused(400, "Cannot cancel order at this stage", can_cancel=False)
        if (
            order.status in AFTER_PURCHASE
            and reason not in NO_RESPONSE_REASONS
            and reason != RETURNED_TO_SHOP
        ):
            raise OrderRefused(409, "after_purchase_resell_or_return", status=order.status)
        if reason in NO_RESPONSE_REASONS:
            if no_response_refresh is not None:
                await no_response_refresh(session, order)
            case = await latest_case(session, order.id)
            verified_no_response = no_response_gate(order.status, case, now)["ok"]
        shop_closed = reason == SHOP_CLOSED and order.status in BEFORE_PURCHASE
        if order.status in AFTER_PURCHASE and reason in NO_RESPONSE_REASONS and not verified_no_response:
            raise OrderRefused(409, "after_purchase_resell_or_return", status=order.status)
        if (
            order.status in PENALTY_STATUSES
            and not verified_no_response
            and not verified_stock_check
            and not shop_closed
        ):
            mine.late_cancellations += 1
            emit(session, "CourierProfile", "update", mine.id)
        if case is None:
            case = await latest_case(session, order.id)

    if case is not None and case.status != "resolved" and cancelled_by == "courier":
        was_counted = case.incident_counted
        case.status, case.resolved_at = "resolved", now
        if verified_no_response:
            case.resolution = "returned_to_shop" if reason == "goods_returned_to_shop" else "cancelled_kept"
            case.incident_counted = case.messaging_status != "legacy"  # a legacy case never counts
            case.final_at = case.final_at or now
        else:
            case.resolution, case.incident_counted = "courier_cancelled_other", False
        emit(session, "NoResponseCase", "update", case.id)
        if was_counted != case.incident_counted:
            await mirror_incidents(session, order.customer_id)

    previous_courier_id = order.courier_id
    deal = await linked_hot_deal(session, order)
    order.cancel_reason, order.cancelled_by = reason, cancelled_by
    final_for_courier = cancelled_by == "courier" and (
        (reason == SHOP_CLOSED and order.status in BEFORE_PURCHASE) or order.status in AFTER_PURCHASE
    )
    back_to_pool = (
        cancelled_by == "courier"
        and deal is None
        and not verified_no_response
        and not verified_stock_check
        and not final_for_courier
    )
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

    await stock_checks.close_pending(session, order)

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

    if verified_stock_check:
        texts = order_texts.stock_cancel_for_customer()
    elif final_for_courier and not verified_no_response:
        texts = order_texts.courier_final_cancel(reason, shop_name)
    else:
        texts = order_texts.cancelled_by_courier(reason, verified_no_response, deal is not None)
    await notify_always_pushed(
        session,
        user_id=order.customer_id,
        type_="order_cancelled",
        order_id=order.id,
        metadata={
            "reason": reason,
            "cancelled_by": cancelled_by,
            "recipient_role": "customer",
            "fault_free": verified_stock_check or (final_for_courier and not verified_no_response),
        },
        **texts,
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


# --- releaseBlockedCourier ---------------------------------------------------------------------------

# Before the purchase: the courier has spent nothing yet, the order can go to someone else.
RELEASABLE_AFTER_BLOCK = ("accepted", "at_shop", "price_confirmation_needed")
RELEASE_REASON = "blocked_by_customer"


async def release_blocked_courier(session: AsyncSession, user: CurrentUser, payload: dict[str, Any]) -> dict:
    """The customer blocked the courier of his order and chose "Chercher un autre livreur": the
    courier leaves the order (no penalty for anyone), it goes back to 'pending' and is offered to
    the other couriers (never to the blocked one). Only before the purchase."""
    order = await ot.lock_order(session, payload.get("order_id"))
    if order is None:
        raise OrderRefused(404, "order_not_found")
    if order.customer_id != user.id:
        raise OrderRefused(403, "Unauthorized")
    courier = await session.get(Courier, order.courier_id) if order.courier_id else None
    if courier is None:
        raise OrderRefused(409, "no_courier")
    if order.status not in RELEASABLE_AFTER_BLOCK:
        raise OrderRefused(409, "already_purchased", status=order.status)
    if not await is_blocked(session, user.id, courier.user_id):
        raise OrderRefused(409, "not_blocked")

    now = ot.now_utc()
    await ot.transition(session, order, "pending", user, "releaseBlockedCourier", RELEASE_REASON,
                        cancelled_by="customer")  # fmt: skip
    order.courier_id = None
    order.delivery_fee = None
    # the app tells "the courier cancelled" from cancelled_by: this one is the customer's choice
    order.cancel_reason, order.cancelled_by = RELEASE_REASON, "customer"
    await _reset_stops(session, order)
    await ot.clear_live_position(session, order.id)
    await session.flush()
    await stock_checks.close_pending(session, order)
    await session.execute(
        OrderOffer.__table__.update()
        .where(
            OrderOffer.order_id == order.id,
            OrderOffer.courier_id == courier.id,
            OrderOffer.status.in_(("pending", "accepted")),
        )
        .values(status="expired", decided_at=now)
    )
    for offer_id in (
        await session.execute(
            select(OrderOffer.id).where(OrderOffer.order_id == order.id, OrderOffer.courier_id == courier.id)
        )
    ).scalars():
        emit(session, "OrderOffer", "update", offer_id)
    stop = await first_stop(session, order.id)
    await notify_always_pushed(
        session,
        user_id=courier.user_id,
        type_="order_cancelled",
        order_id=order.id,
        metadata={"reason": RELEASE_REASON, "cancelled_by": "customer", "recipient_role": "courier"},
        **order_texts.released_after_block(stop.name if stop else None),
    )
    if not is_test_order(order.items_text):
        try:  # the release is done: a failed broadcast must not undo it
            async with session.begin_nested():
                await dispatch_order(session, order)
        except Exception:
            log.exception("releaseBlockedCourier: dispatch failed for %s", order.id)
    return {"success": True, "status": order.status}


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
    if status in CUSTOMER_DELAY_CANCEL_STATUSES or status in ("price_confirmation_needed", "on_the_way"):
        # Owner's rule (06/10, QA B25): free until the courier is at the shop, then no cancel button
        return {
            "can_cancel": False,
            "is_delay_cancel": True,
            "penalty_applies": False,
            "reason_code": "courier_at_shop",
            "message_ar": "المندوب في المتجر أو اشترى المواد: لم يعد الإلغاء ممكناً. اتصل بالمندوب أو بالدعم.",
            "message_fr": (
                "Le livreur est au magasin ou a déjà acheté vos articles : l'annulation n'est plus possible. "
                "Contactez le livreur ou le support."
            ),
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
    if status in AFTER_PURCHASE:
        return {
            "can_cancel": False,
            "is_delay_cancel": True,
            "penalty_applies": True,
            "after_purchase": True,
            "reason_code": "courier_after_purchase",
            "message_ar": "اشتريت المواد: أعد بيعها كعرض ساخن لاسترجاع مالك، أو أرجعها للمتجر.",
            "message_fr": (
                "Vous avez déjà payé les articles : revendez-les en Offre Chaude pour récupérer "
                "votre argent, ou rendez-les au magasin."
            ),
        }
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
