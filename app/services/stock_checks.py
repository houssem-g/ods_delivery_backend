"""'Article indisponible' (stock check): the courier at the shop cannot find an item the customer
asked for; the customer decides, the server enforces the deadline. New in the own backend
(owner's request, 2026-09-29), no Base44 counterpart.

  report   reportUnavailableItems, the assigned courier, before the purchase (accepted / at_shop /
           price_confirmation_needed; never a hot deal: its goods are already bought). Opens a
           check (deadline = now + WAIT_SECONDS), order → price_confirmation_needed (via at_shop
           from accepted), a line in the order chat, an always-pushed `stock_check` to the
           customer (vibrating Android channel), an in-app notice to the admins. At most one
           pending check per order, MAX_CHECKS per order.
  answer   answerStockCheck, the order's customer: accept (only when a substitute was proposed
           and something is available) / skip (not when nothing is available) → back to
           at_shop; cancel → the order is cancelled by the customer, reason product_unavailable,
           without any penalty. Allowed while the check is pending (even a few seconds past
           the deadline, before the timeout ran: first come, first served) or expired (a late
           answer while the order still waits in price_confirmation_needed).
  advance  the timeout (job `stock_check_timeout`, every minute, plus lazily before a courier's
           product_unavailable cancellation and a new report): the order's unavailable_policy
           applies — substitute → accepted if one was proposed, else skipped; skip → skipped;
           cancel → cancelled by the system; call_me → 'expired' (no decision: the courier calls
           the customer or cancels). Nothing available: cancel → cancelled, anything else →
           expired.
  courier_may_cancel_free
           cancelOrder's proof for a courier's product_unavailable cancellation: the latest check
           of this courier on this order is 'expired' and the order still waits on it. Then the
           order is cancelled (not re-dispatched: the shop does not have it) and nobody is
           penalised.

Concurrency: every action holds the order row lock (`ot.lock_order`); the job locks with SKIP
LOCKED, so a customer answering at that second wins. Every change emits an `Order` update: the
Order document carries the checks (`stock_check`, `stock_checks`).
"""

import logging
import math
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import File, Message, Order, OrderOffer, OrderStockCheck, OrderStop, User
from app.realtime.events import emit
from app.security.deps import CurrentUser
from app.services import order_texts
from app.services import order_transitions as ot
from app.services.geo import as_float
from app.services.notifications import notify
from app.services.offers import close_pending_offers
from app.services.order_notices import notify_always_pushed
from app.services.orders import OrderRefused, courier_of_user, courier_user_id

log = logging.getLogger("odsd.stock_checks")

WAIT_SECONDS = 300
MAX_CHECKS = 5
MAX_TEXT = 500
MAX_PRICE = Decimal("2000")
MAX_QUANTITY = 100
REPORTABLE = ("accepted", "at_shop", "price_confirmation_needed")
WAITING = "price_confirmation_needed"
DECISIONS = {"accept": "substitute_accepted", "skip": "item_skipped", "cancel": "order_cancelled"}
REASON = "product_unavailable"
SOURCE_REPORT = "reportUnavailableItems"
SOURCE_ANSWER = "answerStockCheck"
SOURCE_TIMEOUT = "stockCheckTimeout"
SWEEP_LIMIT = 100

Result = tuple[int, dict[str, Any]]


def _z(value: datetime) -> str:
    """`new Date(x).toISOString()`."""
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _iso(value: datetime | None) -> str | None:
    return _z(value) if value is not None else None


def photo_url(key: str | None) -> str | None:
    return f"{settings.public_files_base_url}/{key}" if key else None


def view(check: OrderStockCheck, now: datetime | None = None) -> dict[str, Any]:
    """The check as the functions answer it (the Order document carries the same keys)."""
    now = now or ot.now_utc()
    pending = check.status == "pending"
    return {
        "id": str(check.id),
        "order_id": str(check.order_id),
        "status": check.status,
        "missing_text": check.missing_text,
        "substitute_text": check.substitute_text,
        "substitute_price": float(check.substitute_price) if check.substitute_price is not None else None,
        "missing_price": float(check.missing_price) if check.missing_price is not None else None,
        "quantity": check.quantity,
        "photo_url": photo_url(check.photo_key),
        "nothing_available": check.nothing_available,
        "decided_by": check.decided_by,
        "created_at": _z(check.created_at) if check.created_at else None,
        "deadline_at": _z(check.deadline_at),
        "decided_at": _iso(check.decided_at),
        "server_now": _z(now),
        "seconds_left": max(0, math.ceil((check.deadline_at - now).total_seconds())) if pending else 0,
        "can_accept": bool(check.substitute_text) and not check.nothing_available,
        "can_skip": not check.nothing_available,
    }


def _touch(session: AsyncSession, check: OrderStockCheck) -> None:
    """Realtime: the Order document carries the checks."""
    emit(session, "Order", "update", check.order_id)


async def _checks(session: AsyncSession, order_id: uuid.UUID) -> list[OrderStockCheck]:
    """The order's checks, newest first, locked (the caller holds the order lock)."""
    return list(
        (
            await session.execute(
                select(OrderStockCheck)
                .where(OrderStockCheck.order_id == order_id)
                .order_by(OrderStockCheck.created_at.desc(), OrderStockCheck.id.desc())
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )


async def has_pending(session: AsyncSession, order_id: uuid.UUID) -> bool:
    return (
        await session.execute(
            select(func.count())
            .select_from(OrderStockCheck)
            .where(OrderStockCheck.order_id == order_id, OrderStockCheck.status == "pending")
        )
    ).scalar_one() > 0


def _clean(value: Any, limit: int = MAX_TEXT) -> str:
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def _price(value: Any, error: str = "invalid_price") -> Decimal | None:
    if value in (None, ""):
        return None
    number = as_float(value)
    if number is None or number < 0 or Decimal(str(number)) > MAX_PRICE:
        raise OrderRefused(400, error, max=float(MAX_PRICE))
    return Decimal(str(round(number, 3)))


def _quantity(value: Any) -> int:
    if value in (None, ""):
        return 1
    number = as_float(value)
    if number is None or not number.is_integer() or not 1 <= number <= MAX_QUANTITY:
        raise OrderRefused(400, "invalid_quantity", max=MAX_QUANTITY)
    return int(number)


async def _photo_key(session: AsyncSession, url: Any, user: CurrentUser) -> str | None:
    """One of the courier's own public uploads (the upload path of ReportIssueModal)."""
    if url in (None, ""):
        return None
    prefix = f"{settings.public_files_base_url}/"
    if not isinstance(url, str) or len(url) > 500 or not url.startswith(prefix):
        raise OrderRefused(400, "invalid_photo")
    key = url[len(prefix) :]
    if not key.startswith("public/"):
        raise OrderRefused(400, "invalid_photo")
    owner = (await session.execute(select(File.owner_id).where(File.key == key))).first()
    if owner is None or owner[0] != user.id:
        raise OrderRefused(400, "invalid_photo")
    return key


async def _chat(
    session: AsyncSession,
    order: Order,
    *,
    sender_id: uuid.UUID | None,
    recipient_id: uuid.UUID | None,
    sender_role: str,
    body: str,
) -> None:
    """A line in the order chat, so the history keeps it (template: no chat push, the stock
    check notifications already reached the phone)."""
    message = Message(
        order_id=order.id,
        sender_id=sender_id,
        recipient_id=recipient_id,
        sender_role=sender_role,
        body=body[:1000],
        is_template=True,
    )
    session.add(message)
    await session.flush()
    emit(session, "Message", "create", message.id)


async def _mark_current_stop_at_shop(session: AsyncSession, order: Order) -> None:
    stop = (
        await session.execute(
            select(OrderStop)
            .where(OrderStop.order_id == order.id, OrderStop.seq == order.current_stop_seq)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if stop is not None and stop.status in ("pending", "en_route"):
        stop.status = "at_shop"


# ─────────────────────────── report ───────────────────────────


async def report(session: AsyncSession, user: CurrentUser, payload: dict[str, Any]) -> Result:
    order_id = payload.get("order_id")
    nothing = payload.get("nothing_available") is True
    missing = _clean(payload.get("missing_text"))
    if not order_id:
        raise OrderRefused(400, "Missing required fields")
    if not missing:
        if not nothing:
            raise OrderRefused(400, "missing_text_required")
        missing = "Rien n'est disponible / حتى شي مش موجود"
    substitute = None if nothing else (_clean(payload.get("substitute_text")) or None)
    price = None if nothing else _price(payload.get("substitute_price"))
    if price is not None and substitute is None:
        raise OrderRefused(400, "substitute_text_required")
    missing_price = _price(payload.get("missing_price"), "invalid_missing_price")
    quantity = _quantity(payload.get("quantity"))

    order = await ot.lock_order(session, str(order_id))
    if order is None:
        raise OrderRefused(404, "Order not found")
    courier = await courier_of_user(session, user.id)
    if courier is None or order.courier_id != courier.id:
        raise OrderRefused(403, "Unauthorized")
    if payload.get("courier_id") not in (None, "") and str(payload.get("courier_id")) != str(courier.id):
        raise OrderRefused(403, "Unauthorized")
    photo_key = await _photo_key(session, payload.get("photo_url"), user)

    checks = await _checks(session, order.id)
    if checks and checks[0].status == "pending":
        await advance(session, order, checks[0])
    if order.status not in REPORTABLE or order.resale_deal_id is not None:
        raise OrderRefused(409, "not_reportable", status=order.status)
    if any(c.status == "pending" for c in checks):
        raise OrderRefused(409, "stock_check_pending", stock_check=view(checks[0]))
    if len(checks) >= MAX_CHECKS:
        raise OrderRefused(429, "too_many_stock_checks", max=MAX_CHECKS)

    now = ot.now_utc()
    check = OrderStockCheck(
        order_id=order.id,
        courier_id=courier.id,
        missing_text=missing,
        substitute_text=substitute,
        substitute_price=price,
        missing_price=missing_price,
        quantity=quantity,
        photo_key=photo_key,
        nothing_available=nothing,
        status="pending",
        deadline_at=now + timedelta(seconds=WAIT_SECONDS),
        created_at=now,
    )
    session.add(check)
    await session.flush()
    if order.status == "accepted":
        await _mark_current_stop_at_shop(session, order)
        await ot.transition(session, order, "at_shop", user, SOURCE_REPORT)
    if order.status == "at_shop":
        await ot.transition(session, order, WAITING, user, SOURCE_REPORT, "stock_check")
    _touch(session, check)

    await _chat(
        session,
        order,
        sender_id=user.id,
        recipient_id=order.customer_id,
        sender_role="courier",
        body=order_texts.stock_check_chat(missing, substitute, price, nothing),
    )
    minutes = round(WAIT_SECONDS / 60)
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
            "deadline_at": _z(check.deadline_at),
            "nothing_available": nothing,
            "has_substitute": substitute is not None,
        },
        **order_texts.stock_check_for_customer(missing, substitute, price, nothing, minutes),
    )
    admins = (
        await session.execute(select(User.id).where(User.role == "admin", User.deleted_at.is_(None)))
    ).scalars()
    for admin_id in admins:
        await notify(
            session,
            user_id=admin_id,
            type_="issue_reported",
            order_id=order.id,
            push=False,
            metadata={
                "issue_type": "stock_check",
                "description": missing,
                "substitute": substitute or "",
                "nothing_available": nothing,
                "courier_id": str(courier.id),
                "photo_url": photo_url(photo_key) or "",
                "stock_check_id": str(check.id),
            },
            **order_texts.stock_check_for_admin(str(order.id)[-6:].upper(), nothing),
        )
    return 200, {"success": True, "stock_check": view(check, now), "order_status": order.status}


# ─────────────────────────── decisions ───────────────────────────


async def _apply(
    session: AsyncSession,
    order: Order,
    check: OrderStockCheck,
    status: str,
    decided_by: str,
    actor: CurrentUser | None,
) -> None:
    """Records the decision and moves the order (the caller holds the order lock)."""
    now = ot.now_utc()
    check.status, check.decided_by, check.decided_at = status, decided_by, now
    await session.flush()
    source = SOURCE_ANSWER if decided_by == "customer" else SOURCE_TIMEOUT
    if status in ("substitute_accepted", "item_skipped") and order.status == WAITING:
        await ot.transition(session, order, "at_shop", actor, source, status)
    elif status == "order_cancelled" and order.status not in ot.TERMINAL_STATUSES:
        by = "customer" if decided_by == "customer" else "system"
        order.cancelled_by, order.cancel_reason = by, REASON
        await ot.transition(session, order, "cancelled", actor, source, REASON, cancelled_by=by)
        await close_pending_offers(session, OrderOffer.order_id == order.id, "rejected")
    _touch(session, check)

    courier_user = await courier_user_id(session, order.courier_id)
    if decided_by == "customer" and status in order_texts.DECISION_CHAT:
        await _chat(
            session,
            order,
            sender_id=order.customer_id,
            recipient_id=courier_user,
            sender_role="customer",
            body=order_texts.DECISION_CHAT[status],
        )
    if courier_user is not None:
        await notify_always_pushed(
            session,
            user_id=courier_user,
            type_="stock_check_answered",
            order_id=order.id,
            metadata={
                "recipient_role": "courier",
                "stock_check_id": str(check.id),
                "decision": status,
                "decided_by": decided_by,
                "can_cancel_without_penalty": status == "expired",
            },
            **order_texts.stock_decision_for_courier(status, decided_by, check.substitute_text),
        )
    if decided_by == "system":
        await notify_always_pushed(
            session,
            user_id=order.customer_id,
            type_="stock_check",
            order_id=order.id,
            metadata={
                "recipient_role": "customer",
                "stock_check_id": str(check.id),
                "decision": status,
                "decided_by": "system",
            },
            **order_texts.stock_timeout_for_customer(status, check.substitute_text),
        )


def timeout_decision(check: OrderStockCheck, policy: str) -> str:
    """What the order's unavailable_policy decides when nobody answered."""
    if check.nothing_available:
        return "order_cancelled" if policy == "cancel" else "expired"
    if policy == "substitute":
        return "substitute_accepted" if check.substitute_text else "item_skipped"
    if policy == "skip":
        return "item_skipped"
    if policy == "cancel":
        return "order_cancelled"
    return "expired"


async def advance(session: AsyncSession, order: Order, check: OrderStockCheck | None) -> bool:
    """The timeout, enforced by whoever looks first. Idempotent; the caller holds the order lock.
    True when the check changed."""
    if check is None or check.status != "pending":
        return False
    if order.status != WAITING:
        # The order left the wait by another path (courier dropped it, expiry, admin): the check
        # is closed without a decision or a notice.
        check.status, check.decided_by, check.decided_at = (
            "order_cancelled" if order.status == "cancelled" else "expired",
            "system",
            ot.now_utc(),
        )
        await session.flush()
        _touch(session, check)
        return True
    if ot.now_utc() < check.deadline_at:
        return False
    await _apply(session, order, check, timeout_decision(check, order.unavailable_policy), "system", None)
    return True


async def answer(session: AsyncSession, user: CurrentUser, payload: dict[str, Any]) -> Result:
    order_id, check_id, decision = (
        payload.get("order_id"),
        payload.get("stock_check_id"),
        payload.get("decision"),
    )
    if not order_id or not check_id or not decision:
        raise OrderRefused(400, "Missing required fields")
    if decision not in DECISIONS:
        raise OrderRefused(400, "invalid_decision", allowed=list(DECISIONS))
    order = await ot.lock_order(session, str(order_id))
    if order is None:
        raise OrderRefused(404, "Order not found")
    if order.customer_id != user.id:
        raise OrderRefused(403, "Unauthorized")
    checks = await _checks(session, order.id)
    check = next((c for c in checks if str(c.id) == str(check_id).strip()), None)
    if check is None:
        raise OrderRefused(404, "stock_check_not_found")
    latest = checks[0]
    open_to_answer = check.status == "pending" or (
        check.status == "expired" and check is latest and order.status == WAITING
    )
    if not open_to_answer or order.status != WAITING:
        raise OrderRefused(409, "already_decided", stock_check=view(check), order_status=order.status)
    if decision == "accept" and (check.nothing_available or not check.substitute_text):
        raise OrderRefused(409, "accept_not_allowed")
    if decision == "skip" and check.nothing_available:
        raise OrderRefused(409, "skip_not_allowed")
    await _apply(session, order, check, DECISIONS[decision], "customer", user)
    return 200, {"success": True, "stock_check": view(check), "order_status": order.status}


# ─────────────────────────── cancelOrder hooks ───────────────────────────


async def refresh(session: AsyncSession, order: Order) -> None:
    """Brings the order's pending check up to date (the order is locked by the caller)."""
    checks = await _checks(session, order.id)
    if checks:
        await advance(session, order, checks[0])


async def courier_may_cancel_free(session: AsyncSession, order: Order, courier_id: uuid.UUID) -> bool:
    """A courier's product_unavailable cancellation is fault-free only with proof: his latest
    check on this order went unanswered ('expired') and the order still waits on it."""
    await refresh(session, order)
    if order.status != WAITING:
        return False
    checks = await _checks(session, order.id)
    return bool(checks) and checks[0].status == "expired" and checks[0].courier_id == courier_id


async def close_pending(session: AsyncSession, order: Order) -> None:
    """cancelOrder (any other path): a pending check of the order closes with it."""
    for check in await _checks(session, order.id):
        if check.status == "pending":
            await advance(session, order, check)


# ─────────────────────────── timeout job ───────────────────────────


async def due_order_ids(session: AsyncSession) -> list[uuid.UUID]:
    """Orders with a pending check past its deadline, or whose order left the wait."""
    return list(
        (
            await session.execute(
                select(OrderStockCheck.order_id)
                .join(Order, Order.id == OrderStockCheck.order_id)
                .where(
                    OrderStockCheck.status == "pending",
                    or_(OrderStockCheck.deadline_at <= func.now(), Order.status != WAITING),
                )
                .order_by(OrderStockCheck.deadline_at)
                .limit(SWEEP_LIMIT)
            )
        ).scalars()
    )


async def sweep_order(session: AsyncSession, order_id: uuid.UUID) -> bool:
    """One order (its own transaction). SKIP LOCKED: a customer answering right now wins."""
    order = (
        await session.execute(
            select(Order)
            .where(Order.id == order_id)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if order is None:
        return False
    checks = await _checks(session, order.id)
    return bool(checks) and await advance(session, order, checks[0])
