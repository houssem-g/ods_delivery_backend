"""Vitrine orders (contract §5, §7): the cart priced again server side, the order sent to the
linked ODS restaurant once the customer accepts an offer, and the restaurant's « plat manquant ».

placeOrder with `order.vitrine` keeps the Delivery order model (SPEC §2.6): the cart becomes the
order's `items_text` (`[Vitrine] <name>` + one line per item + `Total articles : …`),
`estimated_price = budget_max = cart total`, the shop is the Vitrine's; a Vitrine that is not public
(qa, test snapshot, switch off) gives a « QA TEST » order, offered to the QA couriers only (existing
path). The prices are the CURRENT catalog's: a price the client sends is only compared (409
prices_changed), never used.

Webhooks: only for a Vitrine linked to an active ODS restaurant, from the first acceptance on
(`vitrine.order_confirmed`, once), then `vitrine.order_updated` / `vitrine.order_cancelled` to the
partner that got the confirmation. Same queue, signature and retries as the Lot 2 events
(app/services/partner_webhooks.py); `data.order` is the Vitrine view (no customer data at all).
"""

from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ApiError
from app.models import (
    Order,
    OrderStatusEvent,
    OrderStockCheck,
    Partner,
    PartnerWebhookDelivery,
    Shop,
    User,
    Vitrine,
    VitrineCategory,
    VitrineProduct,
)
from app.realtime.events import emit
from app.security.deps import to_current_user
from app.services import order_transitions as ot
from app.services import partner_webhooks, vitrine_options
from app.services import vitrine as vitrine_service
from app.services.orders import MAX_BUDGET, MAX_QUANTITY, OrderRefused, is_test_order
from app.services.places import lat_expr, lng_expr

LABEL = "[Vitrine]"
TEST_PREFIX = "QA TEST "
MAX_LINES = 40
MAX_LINE_QTY = 50
ITEMS_TEXT_MAX = 2000
CONFIRMED = "vitrine.order_confirmed"
UPDATED = "vitrine.order_updated"
CANCELLED = "vitrine.order_cancelled"
COURIER_STATE = {
    "pending": "searching",
    "offers_received": "searching",
    "accepted": "assigned",
    "at_shop": "at_restaurant",
    "price_confirmation_needed": "at_restaurant",
    "purchased": "picked_up",
    "on_the_way": "on_the_way",
    "delivered": "delivered",
    "client_no_response": "incident_no_response",
    "cancelled": "none",
}
MISSING_ALLOWED = ("accepted", "at_shop", "price_confirmation_needed")
AFTER_PURCHASE = ("purchased", "on_the_way", "client_no_response")
MISSING_DECISIONS = {
    "item_skipped": "removed",
    "substitute_accepted": "removed",
    "order_cancelled": "cancelled",
    "expired": "no_answer",
}


def dt(value: Decimal) -> str:
    return f"{value:.3f} DT"


def _first_name(full_name: str | None) -> str | None:
    parts = (full_name or "").strip().split()
    return parts[0][:40] if parts else None


# --- placeOrder ---------------------------------------------------------------------------------------


def _lines(raw: Any) -> list[dict[str, Any]]:
    """The cart lines, shape only. One line = (product, removals): the same dish twice with other
    removals is two lines (« sans harissa » for the child), the same dish and removals twice is refused."""
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_LINES:
        raise OrderRefused(400, "invalid_cart", max_lines=MAX_LINES)
    lines, seen = [], set()
    for item in raw:
        line = item if isinstance(item, dict) else {}
        product_id = vitrine_service.as_uuid(line.get("product_id"))
        qty = line.get("qty")
        mods = vitrine_options.parse_mods(line.get("mods"))
        if product_id is None or mods is None or (product_id, vitrine_options.signature(mods)) in seen:
            raise OrderRefused(400, "invalid_cart", max_lines=MAX_LINES)
        if isinstance(qty, bool) or not isinstance(qty, int) or not 1 <= qty <= MAX_LINE_QTY:
            raise OrderRefused(400, "invalid_cart", max_qty=MAX_LINE_QTY)
        seen.add((product_id, vitrine_options.signature(mods)))
        unit_price = None
        if line.get("unit_price") not in (None, ""):
            unit_price = vitrine_service.money(line.get("unit_price"))
            if unit_price is None:
                unit_price = Decimal("-1")  # nonsense: certainly « changed »
        lines.append(
            {
                "product_id": product_id,
                "qty": qty,
                "note": vitrine_service.clean(line.get("note"), vitrine_service.NOTE_MAX) or None,
                "unit_price": unit_price,
                "mods": mods,
            }
        )
    return lines


def items_text(name: str, cart_lines: list[dict[str, Any]], total: Decimal, test: bool) -> str:
    """`[Vitrine] <name>` + `2× Café crème — 5.000 DT (précision)` … + `Total articles : …` (≤ 2000)."""

    def build(with_notes: bool, name_max: int, with_mods: bool = True) -> str:
        rows = [f"{LABEL} {name}"]
        for line in cart_lines:
            note = f" ({line['note']})" if with_notes and line.get("note") else ""
            mods = f" · {line['mods_text']}" if with_mods and line.get("mods_text") else ""
            rows.append(
                f"{line['qty']}× {line['name'][:name_max]} — {dt(Decimal(line['line_total']))}{mods}{note}"
            )
        rows.append(f"Total articles : {dt(total)}")
        text = "\n".join(rows)
        return TEST_PREFIX + text if test else text

    # the removals go before the notes: they are what the shop must not miss
    for with_notes, name_max, with_mods in (
        (True, 100, True),
        (False, 100, True),
        (False, 40, True),
        (False, 40, False),
    ):
        text = build(with_notes, name_max, with_mods)
        if len(text) <= ITEMS_TEXT_MAX:
            return text
    raise OrderRefused(400, "invalid_cart", reason="too_long")


async def prepare(
    session: AsyncSession, user: User, raw: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The placeOrder body rebuilt from the Vitrine's current catalog, and the order's Vitrine
    fields. Raises OrderRefused (404 / 409 / 400 of the contract §5)."""
    v = raw.get("vitrine") if isinstance(raw.get("vitrine"), dict) else {}
    viewer = to_current_user(user)
    try:
        shop, vitrine = await vitrine_service.find_visible(
            session, viewer, shop_id=v.get("shop_id"), slug=v.get("slug")
        )
    except ApiError as exc:
        raise OrderRefused(exc.status, exc.error) from None
    lines = _lines(v.get("lines"))
    state = vitrine_service.open_state(vitrine.hours)
    if state["open_now"] is False:
        raise OrderRefused(409, "shop_closed", opens_at=state["opens_at"])
    partner = await session.get(Partner, vitrine.partner_id) if vitrine.partner_id else None
    if vitrine.source == "ods" and partner is not None and partner.debt_amount and partner.debt_amount > 0:
        raise OrderRefused(409, "restaurant_debt_unpaid", amount=float(partner.debt_amount))
    products = {
        p.id: p
        for p in (
            await session.execute(
                select(VitrineProduct).where(
                    VitrineProduct.shop_id == shop.id,
                    VitrineProduct.id.in_([line["product_id"] for line in lines]),
                )
            )
        ).scalars()
    }
    per_product: dict[Any, int] = {}
    for line in lines:  # the same dish on several lines (other removals): 50 at most in all
        per_product[line["product_id"]] = per_product.get(line["product_id"], 0) + line["qty"]
    if any(q > MAX_LINE_QTY for q in per_product.values()):
        raise OrderRefused(400, "invalid_cart", max_qty=MAX_LINE_QTY)
    unavailable = [
        str(line["product_id"])
        for line in lines
        if (p := products.get(line["product_id"])) is None or p.archived_at is not None or not p.available
    ]
    if unavailable:
        raise OrderRefused(409, "items_unavailable", product_ids=unavailable)
    changed = [
        {
            "product_id": str(line["product_id"]),
            "name": products[line["product_id"]].name,
            "old_price": float(line["unit_price"]) if line["unit_price"] >= 0 else None,
            "new_price": float(products[line["product_id"]].price),
        }
        for line in lines
        if line["unit_price"] is not None and line["unit_price"] != products[line["product_id"]].price
    ]
    if changed:
        changed = list({c["product_id"]: c for c in changed}.values())  # one row per dish
        raise OrderRefused(409, "prices_changed", lines=changed)
    # the removals: only the ones this dish offers now (the server's list, never the client's)
    options = await _options_of(session, shop, [products[line["product_id"]] for line in lines])
    resolved, stale = [], []
    for line in lines:
        product = products[line["product_id"]]
        mods, bad = vitrine_options.resolve(line["mods"], options.get(product.id, []))
        if bad and str(product.id) not in {x["product_id"] for x in stale}:
            stale.append({"product_id": str(product.id), "name": product.name, "keys": bad})
        resolved.append(mods)
    if stale:
        raise OrderRefused(409, "options_changed", lines=stale)
    cart_lines = []
    total = Decimal("0")
    for line, mods in zip(lines, resolved, strict=True):
        product = products[line["product_id"]]
        line_total = product.price * line["qty"]  # a removal never changes the price (price_delta 0)
        total += line_total
        cart_line = {
            "product_id": str(product.id),
            "ods_menu_id": product.ods_menu_id,
            "name": product.name,
            "qty": line["qty"],
            "unit_price": str(product.price),
            "line_total": str(line_total),
            "note": line["note"],
        }
        if mods:
            cart_line["mods"] = mods
            cart_line["mods_text"] = vitrine_options.mods_text(mods)
        cart_lines.append(cart_line)
    if total > MAX_BUDGET:
        raise OrderRefused(400, "invalid_budget", max=float(MAX_BUDGET))
    allowed = await vitrine_service.public_allowed(session)
    test = not vitrine_service.effectively_public(shop, vitrine, allowed)
    lat, lng = (
        await session.execute(
            select(lat_expr(Shop.location), lng_expr(Shop.location)).where(Shop.id == shop.id)
        )
    ).one()
    rebuilt = {
        key: raw.get(key)
        for key in (
            "notes", "unavailable_policy", "package_size", "delivery_address", "delivery_lat", "delivery_lng",
            "delivery_details", "delivery_governorate", "delivery_city", "customer_phone", "preferred_time",
            "scheduled_time",
        )
    }  # fmt: skip
    rebuilt.update(
        {
            "items_text": items_text(shop.name, cart_lines, total, test),
            "quantity": min(sum(line["qty"] for line in lines), MAX_QUANTITY),
            "estimated_price": float(total),
            "budget_max": float(total),
            "shop_name": shop.name,
            "shop_address": shop.address,
            "shop_phone": shop.phone,
            "shop_governorate": shop.governorate,
            "shop_city": shop.city,
            "shop_lat": float(lat),
            "shop_lng": float(lng),
        }
    )
    src = v.get("src")
    extras = {
        "shop_id": shop.id,
        "cart": {
            "shop_id": str(shop.id),
            "source": vitrine.source,
            "catalog_version": vitrine.catalog_version,
            "test": test,
            "items_total": str(total),
            "lines": cart_lines,
        },
        "src": (vitrine_service.clean(src, 32).lower() or None) if isinstance(src, str) else None,
    }
    return rebuilt, extras


async def _options_of(session: AsyncSession, shop: Shop, products: list[VitrineProduct]) -> dict[Any, list]:
    """{product id: every removal it allows now} (engine + admin corrections, uncapped)."""
    category_ids = {p.category_id for p in products if p.category_id is not None}
    names = (
        dict(
            (
                await session.execute(
                    select(VitrineCategory.id, VitrineCategory.name).where(
                        VitrineCategory.id.in_(category_ids), VitrineCategory.archived_at.is_(None)
                    )
                )
            ).all()
        )
        if category_ids
        else {}
    )
    kind = shop.categories[0] if shop.categories else None
    return {p.id: vitrine_options.for_product(p, names.get(p.category_id), kind) for p in products}


async def record_options(session: AsyncSession, extras: dict[str, Any]) -> None:
    """placeOrder: the removals ordered feed the chips' order (aggregated counts only)."""
    cart = extras.get("cart") or {}
    if cart.get("test"):
        return  # a test order (QA, Vitrine not public) never orders the real customers' chips
    lines = cart.get("lines") or []
    if any(line.get("mods") for line in lines):
        await vitrine_options.record(session, extras["shop_id"], lines)


def apply_extras(order: Order, stops: list[Any], extras: dict[str, Any]) -> None:
    order.source = "vitrine"
    order.vitrine_shop_id = extras["shop_id"]
    order.cart = extras["cart"]
    order.vitrine_src = extras["src"]
    if stops:
        stops[0].shop_id = extras["shop_id"]


# --- the partner side ---------------------------------------------------------------------------------


async def confirmed_partner(session: AsyncSession, order: Order) -> Partner | None:
    """The restaurant that received `vitrine.order_confirmed` for this order (gets all the rest)."""
    partner_id = (
        await session.execute(
            select(PartnerWebhookDelivery.partner_id)
            .where(PartnerWebhookDelivery.order_id == order.id, PartnerWebhookDelivery.event == CONFIRMED)
            .limit(1)
        )
    ).scalar_one_or_none()
    return await session.get(Partner, partner_id) if partner_id is not None else None


async def linked_partner(session: AsyncSession, order: Order) -> Partner | None:
    """The active ODS restaurant of the order's Vitrine (None: manual Vitrine, unlinked, revoked)."""
    if order.vitrine_shop_id is None:
        return None
    vitrine = await session.get(Vitrine, order.vitrine_shop_id)
    if vitrine is None or vitrine.source != "ods" or vitrine.partner_id is None:
        return None
    partner = await session.get(Partner, vitrine.partner_id)
    if partner is None or not partner.active or partner.revoked_at is not None:
        return None
    return partner


def _lines_for_ods(order: Order) -> list[dict[str, Any]]:
    out = []
    for line in (order.cart or {}).get("lines") or []:
        row = {
            "ods_menu_id": line.get("ods_menu_id"),
            "name": line.get("name"),
            "qty": line.get("qty"),
            "unit_price": float(line.get("unit_price") or 0),
            "note": line.get("note"),
        }
        if line.get("mods"):
            # « ce que j'enlève »: the kitchen reads them in French (CONTRAT-OPTIONS §4)
            row["mods"] = [
                {"key": m.get("key"), "level": m.get("level"), "label": m.get("label_fr")}
                for m in line["mods"]
            ]
            row["mods_text"] = line.get("mods_text")
        out.append(row)
    return out


async def _courier(session: AsyncSession, order: Order) -> dict[str, Any] | None:
    from app.services import partner_orders

    return await partner_orders.courier_card(session, order.courier_id, contact=True)


async def order_view(session: AsyncSession, order: Order) -> dict[str, Any]:
    """`data.order` of the Vitrine events: no customer data (name, phone, address) at all."""
    cancelled = order.status == "cancelled"
    return {
        "vitrine_ref": str(order.id),
        "status": order.status,
        "courier_state": COURIER_STATE.get(order.status, "none"),
        "test": is_test_order(order.items_text),
        "items_total": float((order.cart or {}).get("items_total") or order.estimated_price or 0),
        "delivery_fee": float(order.delivery_fee) if order.delivery_fee is not None else None,
        "purchase_amount": float(order.purchase_amount) if order.purchase_amount is not None else None,
        "courier": await _courier(session, order),
        "eta_minutes": order.eta_minutes,
        "accepted_at": partner_webhooks.iso(order.accepted_at),
        "delivered_at": partner_webhooks.iso(order.delivered_at),
        "cancelled_at": partner_webhooks.iso(order.cancelled_at) if cancelled else None,
        "cancelled_by": order.cancelled_by if cancelled else None,
        "cancel_reason": order.cancel_reason if cancelled else None,
    }


async def _confirmation(session: AsyncSession, order: Order) -> dict[str, Any]:
    cart = order.cart or {}
    return {
        "vitrine_ref": str(order.id),
        "shop_id": str(order.vitrine_shop_id),
        "lines": _lines_for_ods(order),
        "items_total": float(cart.get("items_total") or 0),
        "extra_text": order.notes or None,
        "customer": {"first_name": _first_name(order.contact_name)},
        "courier": await _courier(session, order),
        "delivery_fee": order.delivery_fee,
        "eta_minutes": order.eta_minutes,
        "test": is_test_order(order.items_text),
        "pay_at_counter": True,
        "catalog_version": cart.get("catalog_version"),
    }


def _updated(order: Order, state: str, **extra: Any) -> tuple[str, dict[str, Any]]:
    return UPDATED, {
        "vitrine_ref": str(order.id),
        "courier_state": state,
        "purchase_amount": order.purchase_amount,
        **extra,
    }


async def _at_shop_before(session: AsyncSession, order: Order) -> bool:
    count = (
        await session.execute(
            select(func.count())
            .select_from(OrderStatusEvent)
            .where(OrderStatusEvent.order_id == order.id, OrderStatusEvent.to_status == "at_shop")
        )
    ).scalar_one()
    return count > 1  # the event of this very transition is already written


async def on_transition(
    session: AsyncSession,
    order: Order,
    from_status: str | None,
    to_status: str,
    reason: str | None,
    cancelled_by: str | None,
) -> None:
    """Called by order_transitions.transition() for a Vitrine order (locked, flushed)."""
    partner = await confirmed_partner(session, order)
    events: list[tuple[str, dict[str, Any]]] = []
    if to_status == "accepted":
        if from_status == "price_confirmation_needed":
            return  # back from a « plat manquant » question: nothing new for ODS
        if partner is None:
            partner = await linked_partner(session, order)
            if partner is not None:
                events.append((CONFIRMED, await _confirmation(session, order)))
        else:  # a new courier after the first one dropped the delivery
            events.append(_updated(order, "assigned", courier=await _courier(session, order)))
    elif partner is None:
        return
    elif to_status == "pending" and cancelled_by:
        events.append(_updated(order, "searching"))
    elif to_status in ("at_shop", "purchased", "on_the_way", "delivered", "client_no_response"):
        if to_status == "at_shop" and await _at_shop_before(session, order):
            return  # back at the counter after a stock check: ODS already knows
        events.append(_updated(order, COURIER_STATE[to_status]))
    elif to_status == "cancelled":
        if reason in partner_webhooks.RETURN_REASONS:
            events.append(_updated(order, "returned"))
        events.append(
            (
                CANCELLED,
                {
                    "vitrine_ref": str(order.id),
                    "cancelled_by": cancelled_by,
                    "reason": reason,
                    "stage": COURIER_STATE.get(from_status or "", "none"),
                },
            )
        )
    if partner is None:
        return
    for event, data in events:
        await partner_webhooks.queue(session, partner, event, order, data)


async def on_missing_items_decision(session: AsyncSession, order: Order, check: OrderStockCheck) -> None:
    """stock_checks._apply: the customer (or the timeout) answered a restaurant's « plat manquant »."""
    if not check.ods_menu_ids:
        return
    partner = await confirmed_partner(session, order)
    if partner is None:
        return
    decision = MISSING_DECISIONS.get(check.status, "no_answer")
    event, data = _updated(
        order,
        COURIER_STATE.get(order.status, "none"),
        missing_items={"decision": decision, "ods_menu_ids": list(check.ods_menu_ids)},
    )
    await partner_webhooks.queue(session, partner, event, order, data)


# --- POST /api/partner/v1/vitrine-orders/{ref}/missing-items ------------------------------------------


def _missing_texts(shop: str, names: str, minutes: int) -> dict[str, str]:
    return {
        "title_fr": f"🛒 Un article manque chez {shop}",
        "body_fr": (
            f"{shop} n'a plus : {names}. Répondez sous {minutes} min : retirez l'article ou annulez, "
            "sans frais."
        ),
        "title_ar": f"🛒 منتج غير متوفر لدى {shop}",
        "body_ar": (
            f"لم يعد لدى {shop}: {names}. أجب خلال {minutes} دقائق: احذف المنتج أو ألغِ الطلب دون مصاريف."
        ),
    }


async def resume_status(session: AsyncSession, order: Order) -> str:
    """Where an order goes back after a stock check: 'accepted' when the wait began there (a
    restaurant's report before the courier arrived), else 'at_shop'."""
    entered_from = (
        await session.execute(
            select(OrderStatusEvent.from_status)
            .where(
                OrderStatusEvent.order_id == order.id,
                OrderStatusEvent.to_status == "price_confirmation_needed",
            )
            .order_by(OrderStatusEvent.created_at.desc(), OrderStatusEvent.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return "accepted" if entered_from == "accepted" else "at_shop"


async def report_missing(session: AsyncSession, partner: Partner, ref: str, raw: Any) -> dict[str, Any]:
    from app.services import stock_checks
    from app.services.order_notices import notify_always_pushed

    body = raw if isinstance(raw, dict) else {}
    order = await ot.lock_order(session, ref)
    if order is None or order.source != "vitrine":
        raise ApiError(404, "order_not_found", "Order not found")
    confirmed = await confirmed_partner(session, order)
    if confirmed is None or confirmed.id != partner.id:
        raise ApiError(404, "order_not_found", "Order not found")
    ids = body.get("ods_menu_ids")
    if (
        not isinstance(ids, list)
        or not 1 <= len(ids) <= MAX_LINES
        or any(isinstance(i, bool) or not isinstance(i, int) for i in ids)
    ):
        raise ApiError(400, "validation_error", "ods_menu_ids: a list of ODS menu ids")
    by_id = {line.get("ods_menu_id"): line for line in (order.cart or {}).get("lines") or []}
    if any(i not in by_id for i in ids):
        raise ApiError(400, "validation_error", "ods_menu_ids: not in this order")
    ids = list(dict.fromkeys(ids))
    if order.status in AFTER_PURCHASE:
        raise ApiError(409, "already_purchased", "The courier already paid at the counter")
    if order.status not in MISSING_ALLOWED:
        raise ApiError(409, "order_not_open", "The order is not in progress", status=order.status)
    checks = await stock_checks._checks(session, order.id)
    if checks and checks[0].status == "pending":
        await stock_checks.advance(session, order, checks[0])
    if any(c.status == "pending" for c in checks):
        raise ApiError(409, "stock_check_pending", "A question is already waiting for the customer")
    names = ", ".join(str(by_id[i].get("name")) for i in ids)
    missing_text = f"Le restaurant n'a plus : {names}"[: stock_checks.MAX_TEXT]
    if any(c.ods_menu_ids and set(c.ods_menu_ids) & set(ids) for c in checks):
        raise ApiError(409, "item_already_reported", "Already asked for this dish")
    if len(checks) >= stock_checks.MAX_CHECKS:
        raise ApiError(429, "too_many_stock_checks", "Too many questions on this order")
    missing_price = sum(
        Decimal(str(line.get("line_total") or 0))
        for line in (order.cart or {}).get("lines") or []
        if line.get("ods_menu_id") in ids
    )  # the same dish can be on two lines (other removals)
    now = ot.now_utc()
    check = OrderStockCheck(
        order_id=order.id,
        courier_id=order.courier_id,
        missing_text=missing_text,
        missing_price=min(missing_price, MAX_BUDGET),
        quantity=1,
        nothing_available=set(ids) == {k for k in by_id if k is not None},
        status="pending",
        deadline_at=now + timedelta(seconds=stock_checks.WAIT_SECONDS),
        ods_menu_ids=ids,
        created_at=now,
    )
    session.add(check)
    await session.flush()
    if order.status != stock_checks.WAITING:
        await ot.transition(
            session, order, stock_checks.WAITING, None, "vitrineMissingItems", "restaurant_missing_items"
        )
    stock_checks._touch(session, check)
    shop = await session.get(Shop, order.vitrine_shop_id)
    minutes = round(stock_checks.WAIT_SECONDS / 60)
    await notify_always_pushed(
        session,
        user_id=order.customer_id,
        type_="stock_check",
        order_id=order.id,
        metadata={
            "recipient_role": "customer",
            "stock_check_id": str(check.id),
            "requires_action": True,
            "action_type": "answer_stock_check",
            "priority": "high",
            "vibrate": True,
            "deadline_at": partner_webhooks.iso(check.deadline_at),
            "nothing_available": check.nothing_available,
            "has_substitute": False,
            "reported_by": "restaurant",
        },
        **_missing_texts(shop.name if shop else "Le restaurant", names[:120], minutes),
    )
    emit(session, "Order", "update", order.id)
    return {
        "status": "asked",
        "stock_check": {"id": str(check.id), "deadline_at": partner_webhooks.iso(check.deadline_at)},
    }
