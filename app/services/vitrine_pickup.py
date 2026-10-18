"""« Je viens chercher » (click and collect) in a Vitrine linked to ODS (owner, 17/10/2026).

The customer picks « Je viens chercher » instead of « Livraison »: same cart (removals, extras), no courier,
no fee; the order goes straight to the restaurant's ODS (`vitrine.order_confirmed` with
`fulfillment: "pickup"`), the customer pays at the counter. Contract:
ods-audit/livraison-1610/vitrine/click-collect/CONTRAT.md.

- Offered only when the shop's Vitrine is linked to an active ODS restaurant whose last catalog says
  `restaurant.pickup.enabled` (an older ODS never sends it: never a pickup order it would read as a delivery
  without courier), and not in the last 15 minutes before a known closing time.
- Statuses of its own (awaiting_restaurant → preparing → ready → collected, or cancelled): no courier path
  (dispatch, open orders, offers, live tracking) ever reads them.
- The restaurant moves it, BY HAND, from ODS (`POST /api/partner/v1/vitrine-orders/{ref}/pickup`):
  preparing, ready, collected (paid at the counter), refused (before preparing), not_collected (« Pas venu »,
  counted in the « n'a pas payé » badge), notify (« Prévenir le client »: the « prête » notice again).
  Owner 18/10: nothing automatic — no reminder, no minimum delay, no closing by time. The only automatic
  cancellation is when ODS never received the order (its webhook failed for good): the customer must not go
  to a counter that does not know him.
"""

import json
import logging
import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ApiError
from app.models import Order, Partner, PartnerCatalog, Shop, Vitrine
from app.realtime.events import emit
from app.services import order_transitions as ot
from app.services import partner_webhooks
from app.services.orders import OrderRefused, is_test_order

log = logging.getLogger("odsd.vitrine_pickup")

CLOSING_MARGIN_MINUTES = 15
DUPLICATE_WINDOW = timedelta(minutes=10)
MAX_OPEN_PICKUPS = 2
NOTIFY_EVERY = timedelta(minutes=2)
NAME_MAX = 30
OPEN = ("awaiting_restaurant", "preparing", "ready")
RANK = {"awaiting_restaurant": 0, "preparing": 1, "ready": 2, "collected": 3}
STATES = ("preparing", "ready", "collected", "refused", "not_collected", "notify", "cancelled")
REFUSE_REASONS = {
    "busy": ("trop de commandes en ce moment", "الكثير من الطلبات حالياً"),
    "sold_out": ("un plat n'est plus disponible", "طبق لم يعد متوفراً"),
    "closing": ("le restaurant ferme bientôt", "المطعم سيغلق قريباً"),
    "other": (None, None),
}
NOT_COLLECTED = "not_collected"
UNREACHABLE = "restaurant_unreachable"
RESTAURANT_CANCELLED = (
    "restaurant_cancelled"  # cancelled at the till (or a test ended): any open step, never counted
)


# --- availability -------------------------------------------------------------------------------------


def _pickup_settings(snapshot: PartnerCatalog | None) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    pickup = ((snapshot.payload or {}).get("restaurant") or {}).get("pickup")
    return pickup if isinstance(pickup, dict) else None


def _minutes_until(hhmm: str, now: datetime) -> int:
    from app.services.vitrine import TUNIS

    local = now.astimezone(TUNIS)
    h, m = (int(x) for x in hhmm.split(":"))
    return (h * 60 + m - (local.hour * 60 + local.minute)) % 1440


def _minus(hhmm: str, minutes: int) -> str:
    h, m = (int(x) for x in hhmm.split(":"))
    total = (h * 60 + m - minutes) % 1440
    return f"{total // 60:02d}:{total % 60:02d}"


async def info(
    session: AsyncSession, vitrine: Vitrine, state: dict[str, Any] | None = None, now: datetime | None = None
) -> dict[str, Any]:
    """getVitrine's `pickup`: {available, reason, prep_minutes, last_at}. reason: not_linked (manual or
    unlinked Vitrine: « Appeler le magasin » only), off (the restaurant does not take pickup orders now),
    closing (less than 15 min before closing; last_at = the last minute), shop_closed."""
    from app.services.vitrine import open_state

    out: dict[str, Any] = {"available": False, "reason": "not_linked", "prep_minutes": None, "last_at": None}
    if vitrine.source != "ods" or vitrine.partner_id is None:
        return out
    partner = await session.get(Partner, vitrine.partner_id)
    if partner is None or not partner.active or partner.revoked_at is not None or not partner.webhook_url:
        return out
    settings = _pickup_settings(await session.get(PartnerCatalog, vitrine.partner_id))
    if not settings or settings.get("enabled") is not True:
        return {**out, "reason": "off"}
    prep = settings.get("prep_minutes")
    out["prep_minutes"] = prep if isinstance(prep, int) and not isinstance(prep, bool) else None
    state = state or open_state(vitrine.hours, now)
    if state.get("open_now") is False:
        return {**out, "reason": "shop_closed"}
    closes = state.get("closes_at")
    if state.get("open_now") and closes:
        out["last_at"] = _minus(closes, CLOSING_MARGIN_MINUTES)
        if _minutes_until(closes, now or datetime.now(UTC)) <= CLOSING_MARGIN_MINUTES:
            return {**out, "reason": "closing"}
    return {**out, "available": True, "reason": None}


# --- placeOrder -----------------------------------------------------------------------------------------


def wanted(v: dict[str, Any]) -> bool:
    """`order.vitrine.fulfillment`: absent / 'delivery' → the delivery path, unchanged; 'pickup'; else 400."""
    value = v.get("fulfillment")
    if value in (None, "", "delivery"):
        return False
    if value == "pickup":
        return True
    raise OrderRefused(400, "invalid_cart", reason="fulfillment")


async def prepare(
    session: AsyncSession,
    shop: Shop,
    vitrine: Vitrine,
    v: dict[str, Any],
    rebuilt: dict[str, Any],
    user_name: str | None,
) -> dict[str, Any]:
    """The pickup part of vitrine_orders.prepare: 409 pickup_unavailable / pickup_closing, the place where the
    order is handed over = the shop (no column becomes nullable), no time chosen (out of the plan)."""
    from app.services.vitrine import clean, open_state

    state = open_state(vitrine.hours)
    pickup = await info(session, vitrine, state)
    if not pickup["available"]:
        if pickup["reason"] == "closing":
            raise OrderRefused(409, "pickup_closing", last_at=pickup["last_at"])
        if pickup["reason"] == "shop_closed":
            raise OrderRefused(409, "shop_closed", opens_at=state.get("opens_at"))
        raise OrderRefused(409, "pickup_unavailable", reason=pickup["reason"])
    name = clean(v.get("pickup_name"), NAME_MAX) or clean((user_name or "").split(" ")[0], NAME_MAX) or None
    rebuilt.update(
        {
            "delivery_address": rebuilt.get("shop_address") or rebuilt.get("shop_name"),
            "delivery_lat": rebuilt["shop_lat"],
            "delivery_lng": rebuilt["shop_lng"],
            "delivery_details": "",
            "delivery_governorate": rebuilt.get("shop_governorate"),
            "delivery_city": rebuilt.get("shop_city"),
            "preferred_time": None,
            "scheduled_time": None,
        }
    )
    return {"fulfillment": "pickup", "pickup_name": name, "allow_duplicate": v.get("allow_duplicate") is True}


def _cart_signature(cart: dict[str, Any] | None) -> str:
    rows = sorted(
        json.dumps(
            [line.get("product_id"), line.get("qty"), line.get("mods") or [], line.get("extras") or []],
            sort_keys=True,
            default=str,
        )
        for line in (cart or {}).get("lines") or []
    )
    return "|".join(rows)


def tunis_day(now: datetime | None = None) -> Any:
    from app.services.vitrine import TUNIS

    return (now or datetime.now(UTC)).astimezone(TUNIS).date()


async def _free_code(session: AsyncSession, shop_id: Any, day: Any) -> str:
    # two customers of the same shop at the same second: one at a time draws (released at commit)
    await session.execute(select(func.pg_advisory_xact_lock(func.hashtext(f"pickup:{shop_id}:{day}"))))
    used = set(
        (
            await session.execute(
                select(Order.pickup_code).where(
                    Order.vitrine_shop_id == shop_id, Order.pickup_day == day, Order.pickup_code.is_not(None)
                )
            )
        ).scalars()
    )
    free = [f"{n}" for n in range(100, 1000) if f"{n}" not in used]
    if not free:
        raise OrderRefused(409, "pickup_unavailable", reason="no_code_left")
    return secrets.choice(free)


async def check_and_number(session: AsyncSession, order: Order, extras: dict[str, Any]) -> None:
    """place_order, before the order is added: 2 open pickup orders at most per customer (409
    too_many_pickups), the same cart in the same shop within 10 min (409 duplicate_pickup, unless
    allow_duplicate), then the pickup number of the day."""
    # today's open orders only: one the restaurant forgot to close never locks the customer out
    open_rows = list(
        (
            await session.execute(
                select(Order).where(
                    Order.customer_id == order.customer_id,
                    Order.fulfillment == "pickup",
                    Order.status.in_(OPEN),
                    Order.pickup_day == tunis_day(),
                )
            )
        ).scalars()
    )
    if not extras.get("allow_duplicate"):
        since = datetime.now(UTC) - DUPLICATE_WINDOW
        mine = _cart_signature(order.cart)
        for row in open_rows:
            same_cart = _cart_signature(row.cart) == mine
            if row.vitrine_shop_id == order.vitrine_shop_id and row.created_at >= since and same_cart:
                raise OrderRefused(409, "duplicate_pickup", order_id=str(row.id), pickup_code=row.pickup_code)
    if len(open_rows) >= MAX_OPEN_PICKUPS:
        raise OrderRefused(409, "too_many_pickups", max=MAX_OPEN_PICKUPS)
    order.fulfillment = "pickup"
    order.status = "awaiting_restaurant"  # never 'pending' (the couriers' pool), even before ot.start
    order.pickup_name = extras.get("pickup_name")
    order.delivery_fee = Decimal("0")
    order.pickup_day = tunis_day()
    order.pickup_code = await _free_code(session, order.vitrine_shop_id, order.pickup_day)


async def after_start(session: AsyncSession, order: Order) -> None:
    """place_order, once the order is 'awaiting_restaurant': the restaurant's ODS gets it now (queued; the
    job sends it within 30 s, then retries 1 → 16 min)."""
    from app.services import vitrine_orders

    partner = await vitrine_orders.linked_partner(session, order)
    if partner is None or not partner.webhook_url:
        raise OrderRefused(409, "pickup_unavailable", reason="not_linked")
    await partner_webhooks.queue(
        session, partner, vitrine_orders.CONFIRMED, order, await confirmation(session, order)
    )


# --- what ODS receives ---------------------------------------------------------------------------------


async def badge(session: AsyncSession, customer_id: Any) -> dict[str, int] | None:
    """« n'a pas payé X fois sur ses Y dernières commandes » (the 10/10 badge), None when 0 unpaid."""
    from app.services.orders import recent_badge

    total, unpaid = await recent_badge(session, customer_id)
    return {"unpaid": unpaid, "of": total} if unpaid > 0 else None


async def confirmation(session: AsyncSession, order: Order) -> dict[str, Any]:
    from app.services import vitrine_orders

    cart = order.cart or {}
    return {
        "vitrine_ref": str(order.id),
        "shop_id": str(order.vitrine_shop_id),
        "fulfillment": "pickup",
        "pickup_code": order.pickup_code,
        "pickup_name": order.pickup_name,
        "lines": vitrine_orders._lines_for_ods(order),
        "items_total": float(cart.get("items_total") or 0),
        "extra_text": order.notes or None,
        # the first name said at the counter and the badge: never a phone number, an address or an e-mail
        "customer": {
            "first_name": order.pickup_name or vitrine_orders._first_name(order.contact_name),
            "badge": await badge(session, order.customer_id),
        },
        "courier": None,
        "delivery_fee": 0,
        "eta_minutes": None,
        "test": is_test_order(order.items_text),
        "pay_at_counter": True,
        "catalog_version": cart.get("catalog_version"),
    }


async def on_transition(
    session: AsyncSession, order: Order, from_status: str | None, to_status: str, reason: str | None,
    cancelled_by: str | None,
) -> None:  # fmt: skip
    """vitrine_orders.on_transition for a pickup order: ODS hears of a cancellation it did not make itself."""
    from app.services import vitrine_orders

    if to_status != "cancelled" or cancelled_by == "restaurant":
        return
    partner = await vitrine_orders.confirmed_partner(session, order)
    if partner is None:
        return
    data = {
        "vitrine_ref": str(order.id),
        "fulfillment": "pickup",
        "cancelled_by": cancelled_by,
        "reason": reason,
        "stage": from_status,
    }
    await partner_webhooks.queue(session, partner, vitrine_orders.CANCELLED, order, data)


# --- the restaurant's steps (ODS → Delivery) -----------------------------------------------------------


def _money(value: Any) -> str:
    return f"{Decimal(str(value or 0)):.3f}"


async def _shop_name(session: AsyncSession, order: Order) -> str:
    shop = await session.get(Shop, order.vitrine_shop_id) if order.vitrine_shop_id else None
    return shop.name if shop is not None else "Le restaurant"


async def _tell(session: AsyncSession, order: Order, type_: str, texts: dict[str, str], **meta: Any) -> None:
    from app.services.order_notices import notify_always_pushed

    await notify_always_pushed(
        session,
        user_id=order.customer_id,
        type_=type_,
        order_id=order.id,
        metadata={
            "recipient_role": "customer",
            "fulfillment": "pickup",
            "pickup_code": order.pickup_code,
            **meta,
        },
        **texts,
    )


async def notify_ready(session: AsyncSession, order: Order, *, again: bool = False) -> None:
    shop = await _shop_name(session, order)
    total = _money((order.cart or {}).get("items_total"))
    await _tell(
        session,
        order,
        "pickup_ready",
        {
            "title_fr": f"Commande prête · n° {order.pickup_code}",
            "body_fr": f"Venez la chercher chez {shop}. À payer au comptoir : {total} DT",
            "title_ar": f"طلبك جاهز · رقم {order.pickup_code}",
            "body_ar": f"تفضّل باستلامه عند {shop}. للدفع عند الشبّاك: {total} د.ت",
        },
        again=again,
    )
    order.ready_notified_at = datetime.now(UTC)


async def _notify_preparing(session: AsyncSession, order: Order) -> None:
    shop = await _shop_name(session, order)
    await _tell(
        session,
        order,
        "order_preparing",
        {
            "title_fr": f"En préparation · n° {order.pickup_code}",
            "body_fr": f"{shop} prépare votre commande.",
            "title_ar": f"قيد التحضير · رقم {order.pickup_code}",
            "body_ar": f"{shop} يحضّر طلبك.",
        },
    )


async def _notify_cancelled(session: AsyncSession, order: Order, kind: str, why: str | None = None) -> None:
    shop = await _shop_name(session, order)
    why_fr, why_ar = REFUSE_REASONS.get(why or "other", (None, None))
    if kind == "refused":
        texts = {
            "title_fr": "Commande non préparée",
            "body_fr": f"{shop} ne peut pas préparer votre commande maintenant"
            + (f" ({why_fr})" if why_fr else "")
            + ". Rien à payer.",
            "title_ar": "لم يتمّ تحضير الطلب",
            "body_ar": f"لا يستطيع {shop} تحضير طلبك الآن"
            + (f" ({why_ar})" if why_ar else "")
            + ". لا شيء للدفع.",
        }
    elif kind == RESTAURANT_CANCELLED:
        texts = {
            "title_fr": "Commande annulée",
            "body_fr": f"{shop} a annulé votre commande. Rien à payer.",
            "title_ar": "أُلغي الطلب",
            "body_ar": f"ألغى {shop} طلبك. لا شيء للدفع.",
        }
    elif kind == NOT_COLLECTED:
        texts = {
            "title_fr": f"Commande fermée · n° {order.pickup_code}",
            "body_fr": f"{shop} a fermé votre commande : vous n'êtes pas venu la chercher.",
            "title_ar": f"تمّ إغلاق الطلب · رقم {order.pickup_code}",
            "body_ar": f"أغلق {shop} طلبك: لم تأتِ لاستلامه.",
        }
    else:  # UNREACHABLE
        texts = {
            "title_fr": "Commande non reçue",
            "body_fr": f"{shop} n'a pas reçu votre commande. Rien à payer. Vous pouvez l'appeler ou "
            "commander "
            "en livraison.",
            "title_ar": "لم يصل الطلب",
            "body_ar": f"لم يتلقَّ {shop} طلبك. لا شيء للدفع. يمكنك الاتصال به أو الطلب مع التوصيل.",
        }
    await _tell(session, order, "order_cancelled", texts, reason=kind, fault_free=kind != NOT_COLLECTED)


async def _cancel(session: AsyncSession, order: Order, reason: str, by: str, source: str) -> None:
    order.cancel_reason, order.cancelled_by = reason, by
    await ot.transition(session, order, "cancelled", None, source, reason, cancelled_by=by)


async def apply_state(session: AsyncSession, partner: Partner, ref: str, raw: Any) -> dict[str, Any]:
    """POST /api/partner/v1/vitrine-orders/{ref}/pickup — {state, reason?}. Idempotent: a step already reached
    (or passed) answers `unchanged`; a late step after a cancellation too (ODS reads `order_status`)."""
    from app.services import vitrine_orders

    body = raw if isinstance(raw, dict) else {}
    state = body.get("state")
    if state not in STATES:
        raise ApiError(400, "validation_error", "state: " + " | ".join(STATES))
    why = body.get("reason")
    if state == "refused" and why not in (None, *REFUSE_REASONS):
        raise ApiError(400, "validation_error", "reason: " + " | ".join(REFUSE_REASONS))
    order = await ot.lock_order(session, ref)
    if order is None or order.source != "vitrine" or order.fulfillment != "pickup":
        raise ApiError(404, "order_not_found", "Order not found")
    confirmed = await vitrine_orders.confirmed_partner(session, order)
    if confirmed is None or confirmed.id != partner.id:
        raise ApiError(404, "order_not_found", "Order not found")
    if order.restaurant_ack_at is None:
        order.restaurant_ack_at = datetime.now(UTC)  # ODS acts on it: it has it
    current = order.status

    def unchanged() -> dict[str, Any]:
        return {"status": "unchanged", "order_status": order.status}

    if current == "cancelled":
        return unchanged()
    if state in ("preparing", "ready", "collected"):
        if RANK.get(current, 9) >= RANK[state]:
            return unchanged()
        await ot.transition(session, order, state, None, "odsPickup")
        if state == "preparing":
            await _notify_preparing(session, order)
        elif state == "ready":
            await notify_ready(session, order)
    elif state == "refused":
        if current != "awaiting_restaurant":
            raise ApiError(409, "invalid_transition", "Already being prepared", order_status=current)
        await _cancel(session, order, f"restaurant_refused:{why or 'other'}", "restaurant", "odsPickup")
        await _notify_cancelled(session, order, "refused", why)
    elif state == "cancelled":
        # cancelled at the till (or a test ended), whatever the step: the customer is told, never counted
        await _cancel(session, order, RESTAURANT_CANCELLED, "restaurant", "odsPickup")
        await _notify_cancelled(session, order, RESTAURANT_CANCELLED)
    elif state == NOT_COLLECTED:
        if current != "ready":
            raise ApiError(409, "invalid_transition", "Only a ready order", order_status=current)
        await _cancel(session, order, NOT_COLLECTED, "restaurant", "odsPickup")
        await _notify_cancelled(session, order, NOT_COLLECTED)
    else:  # notify: « Prévenir le client », by hand
        if current != "ready":
            raise ApiError(409, "invalid_transition", "Only a ready order", order_status=current)
        last = order.ready_notified_at
        if last is not None and datetime.now(UTC) - last < NOTIFY_EVERY:
            return {"status": "unchanged", "order_status": current, "reason": "too_soon"}
        await notify_ready(session, order, again=True)
    await session.flush()
    emit(session, "Order", "update", order.id)
    return {"status": "updated", "order_status": order.status}


# --- the confirmation's fate (partner_webhooks.deliver) ------------------------------------------------


async def on_confirmation_delivered(session: AsyncSession, order: Order | None) -> None:
    if order is not None and order.fulfillment == "pickup" and order.restaurant_ack_at is None:
        order.restaurant_ack_at = datetime.now(UTC)
        emit(session, "Order", "update", order.id)


async def on_confirmation_failed(session: AsyncSession, order_id: Any) -> None:
    """Every attempt failed (about 30 min): the restaurant never got the order — the customer is told,
    nothing to pay. The only automatic step of « Je viens chercher » (owner 18/10: no reminder, no
    closing by time)."""
    order = await ot.lock_order(session, order_id)
    if order is None or order.fulfillment != "pickup" or order.status != "awaiting_restaurant":
        return
    if order.restaurant_ack_at is not None:
        return
    await _cancel(session, order, UNREACHABLE, "system", "pickupUnreachable")
    await _notify_cancelled(session, order, UNREACHABLE)
