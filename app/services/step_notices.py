"""Order notices the server sends itself (reliability: an app closed or killed right after its
call never sent them; the owner got no "offre acceptée" on 2026-09-29).

  new_offer       createOrderOffer → the customer (push per preferences; WhatsApp fallback of
                  notify for a web-only customer)
  order_accepted  acceptOrderOffer → the chosen courier, always pushed on the vibrating channel
  at_shop / purchased / on_the_way
                  the courier's steps (order_steps.courier_step) → the customer, push per
                  preferences
  delivered       → the customer, always pushed

Idempotent: a notice of the same (user, order, type) — for new_offer, of the same offer — written
in the last 120 s is not written again (`notifications.recent_duplicate`); the installed apps
still send the same notices through sendNotificationIfEnabled, which skips them the same way.
A notice that fails never fails the order action (savepoint, logged).
"""

import logging
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Courier, Order, OrderOffer, OrderStop
from app.security.tokens import now_utc
from app.services import order_texts, tracking
from app.services.notifications import notify, recent_duplicate
from app.services.order_notices import notify_always_pushed, notify_and_push

log = logging.getLogger("odsd.step_notices")

STEP_TYPES = ("at_shop", "purchased", "on_the_way", "delivered")


def _stamp() -> str:
    return now_utc().isoformat().replace("+00:00", "Z")


async def _send(
    session: AsyncSession,
    *,
    mode: str,
    user_id: uuid.UUID,
    order_id: uuid.UUID,
    type_: str,
    text: dict[str, str],
    metadata: dict[str, Any],
    offer_id: str | None = None,
) -> bool:
    """Writes the notice unless a duplicate exists. True when written."""
    try:
        async with session.begin_nested():
            if await recent_duplicate(
                session, user_id=user_id, order_id=order_id, type_=type_, offer_id=offer_id
            ):
                return False
            kwargs = {"user_id": user_id, "order_id": order_id, "type_": type_, "metadata": metadata, **text}
            if mode == "always":
                await notify_always_pushed(session, **kwargs)
            elif mode == "push":
                await notify_and_push(session, **kwargs)
            else:
                await notify(session, **kwargs)
        return True
    except Exception:
        log.exception("%s notice lost: order %s user %s", type_, order_id, user_id)
        return False


async def offer_created(session: AsyncSession, order: Order, offer: OrderOffer, courier: Courier) -> bool:
    return await _send(
        session,
        mode="prefs",
        user_id=order.customer_id,
        order_id=order.id,
        type_="new_offer",
        offer_id=str(offer.id),
        text=order_texts.new_offer_for_customer(courier.display_name, offer.proposed_fee, offer.eta_minutes),
        metadata={
            "recipient_role": "customer",
            "offer_id": str(offer.id),
            "courier_id": str(courier.id),
            "courier_name": order_texts.short_name(courier.display_name),
            "proposed_fee": float(offer.proposed_fee),
            "distance_km": float(offer.distance_km) if offer.distance_km is not None else None,
            "eta_minutes": offer.eta_minutes,
            "timestamp": _stamp(),
        },
    )


async def offer_accepted(
    session: AsyncSession, order: Order, offer: OrderOffer, courier_user_id: uuid.UUID
) -> bool:
    shop_name = await _stop_name(session, order.id, 0)
    return await _send(
        session,
        mode="push",
        user_id=courier_user_id,
        order_id=order.id,
        type_="order_accepted",
        text=order_texts.offer_accepted_for_courier(shop_name, order.items_text),
        metadata={
            "recipient_role": "courier",
            "order_id": str(order.id),
            "offer_id": str(offer.id),
            "shop_name": shop_name,
            "vibrate": True,
            "timestamp": _stamp(),
        },
    )


async def courier_step(session: AsyncSession, order: Order, from_status: str, to_status: str) -> bool:
    """The customer's notice of the courier's step `from_status → to_status` (after the transition).
    at_shop only on arrival (accepted → at_shop), not when a stock check sends it back there."""
    if to_status not in STEP_TYPES or (to_status == "at_shop" and from_status != "accepted"):
        return False
    if to_status == "at_shop":
        text = order_texts.at_shop(await _stop_name(session, order.id, order.current_stop_seq))
    else:
        shop = await _stop_name(session, order.id, 0)  # names the order (B60)
        if to_status == "purchased":
            text = order_texts.purchased(order.purchase_amount, shop, await _has_receipt(session, order.id))
        elif to_status == "on_the_way":
            # the ride's real time (same computation as the tracking ring), not the offer's delay (QA B7)
            text = order_texts.on_the_way(await tracking.ride_eta_minutes(session, order), shop)
        else:
            total = (order.purchase_amount or Decimal(0)) + (order.delivery_fee or Decimal(0))
            text = order_texts.delivered(total if order.purchase_amount is not None else None, shop)
    return await _send(
        session,
        mode="always" if to_status == "delivered" else "prefs",
        user_id=order.customer_id,
        order_id=order.id,
        type_=to_status,
        text=text,
        metadata={"recipient_role": "customer", "status": to_status, "timestamp": _stamp()},
    )


async def _stop_name(session: AsyncSession, order_id: uuid.UUID, seq: int) -> str | None:
    return (
        await session.execute(
            select(OrderStop.name).where(OrderStop.order_id == order_id, OrderStop.seq == seq)
        )
    ).scalar_one_or_none()


async def _has_receipt(session: AsyncSession, order_id: uuid.UUID) -> bool:
    """A receipt photo on any shop of the order (« selon le reçu » only then, QA N16)."""
    found = (
        await session.execute(
            select(OrderStop.id)
            .where(
                OrderStop.order_id == order_id,
                OrderStop.receipt_key.is_not(None),
                OrderStop.receipt_key != "",
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return found is not None
