"""Order chat: send, read, mark read, unread list (ports of sendOrderMessage,
getOrderMessages, markOrderMessagesRead, listMyUnreadMessages).

Who may write / read an order's chat (`chat_role`):
  - the order's customer                                         → 'customer'
  - the assigned courier                                         → 'courier'
  - a VERIFIED courier while the order is still open to offers
    (pending / offers_received, no courier yet): questions before or after bidding
    → 'bidder': he sees his own messages and the customer's, never the other couriers'
  - an admin (reading only; sendOrderMessage has no admin role)   → 'admin'
The sender role and the recipient are decided here, never taken from the client.

Every function answers `(status, json)` with the Deno keys and codes.
"""

import logging
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import and_, exists, func, or_, select, true, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.compat.entities.message import ENTITY as MESSAGE_ENTITY
from app.compat.query import base_select, serialize
from app.models import Courier, Message, Order
from app.realtime.events import emit
from app.security.deps import CurrentUser
from app.security.tokens import now_utc
from app.services.notifications import notify

MAX_CONTENT = 1000
PRE_ASSIGN_MAX = 10  # messages a bidder may post on one open order
BURST_MAX = 10  # messages a minute per sender and order
OPEN_STATUSES = ("pending", "offers_received")
ORDERS_PER_SIDE = 50
MAX_UNREAD = 200
PREVIEW_PUSH = 80
PREVIEW_METADATA = 100

Result = tuple[int, dict[str, Any]]
log = logging.getLogger("odsd.messages")
# Reads done on behalf of a function that already checked the caller: no read policy.
SYSTEM = CurrentUser(id=uuid.UUID(int=0), email="", role="admin", full_name="system")

messages = Message.__table__


def _as_uuid(raw: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(raw))
    except ValueError:
        return None


async def load_order(session: AsyncSession, raw_id: str) -> Order | None:
    order_id = _as_uuid(raw_id)
    return await session.get(Order, order_id) if order_id else None


async def courier_user_id(session: AsyncSession, order: Order) -> uuid.UUID | None:
    """The assigned courier's user (orders reference the courier profile)."""
    if order.courier_id is None:
        return None
    return (
        await session.execute(select(Courier.user_id).where(Courier.id == order.courier_id))
    ).scalar_one_or_none()


async def chat_role(
    session: AsyncSession, order: Order, user: CurrentUser, *, admin: bool = True
) -> str | None:
    if admin and user.is_admin:
        return "admin"
    if order.customer_id == user.id:
        return "customer"
    if order.courier_id is not None and await courier_user_id(session, order) == user.id:
        return "courier"
    verification = (
        await session.execute(select(Courier.verification).where(Courier.user_id == user.id))
    ).scalar_one_or_none()
    if verification == "verified" and order.courier_id is None and order.status in OPEN_STATUSES:
        return "bidder"
    return None


def _visible(role: str, user: CurrentUser) -> Any:
    """A bidder sees his own messages and the customer's only."""
    if role != "bidder":
        return true()
    return or_(messages.c.sender_id == user.id, messages.c.sender_role == "customer")


def _incoming_unread(role: str, user: CurrentUser) -> Any:
    """Never the caller's own side's messages (a courier must not mark the customer's
    unread messages of another chat)."""
    side = (
        true()
        if role == "admin"
        else (
            messages.c.sender_role != "customer"
            if role == "customer"
            else messages.c.sender_role == "customer"
        )
    )
    return and_(messages.c.sender_id.is_distinct_from(user.id), messages.c.read_at.is_(None), side)


async def _docs(session: AsyncSession, *where: Any, newest_first: bool, limit: int) -> list[dict[str, Any]]:
    order = messages.c.created_at.desc() if newest_first else messages.c.created_at.asc()
    stmt = base_select(MESSAGE_ENTITY, SYSTEM).where(*where).order_by(order, messages.c.id).limit(limit)
    return [serialize(MESSAGE_ENTITY, row) for row in (await session.execute(stmt)).all()]


# ─────────────────────────── sendOrderMessage ───────────────────────────


async def send_order_message(session: AsyncSession, user: CurrentUser, payload: dict[str, Any]) -> Result:
    raw_order = payload.get("order_id")
    raw_content = payload.get("content")
    order_id = raw_order.strip() if isinstance(raw_order, str) else ""
    content = raw_content.strip() if isinstance(raw_content, str) else ""
    if not order_id:
        return 400, {"error": "Missing order_id"}
    if not content or len(content) > MAX_CONTENT:
        return 400, {"error": "invalid_content", "max": MAX_CONTENT}
    order = await load_order(session, order_id)
    if order is None:
        return 404, {"error": "order_not_found"}
    role = await chat_role(session, order, user, admin=False)
    if role is None:
        return 403, {"error": "not_a_party"}
    if order.status == "cancelled" and role != "customer":
        return 409, {"error": "order_closed"}

    mine = messages.c.order_id == order.id, messages.c.sender_id == user.id
    last_minute = (
        await session.execute(
            select(func.count()).where(*mine, messages.c.created_at >= now_utc() - timedelta(minutes=1))
        )
    ).scalar_one()
    total = (await session.execute(select(func.count()).where(*mine))).scalar_one() if role == "bidder" else 0
    if last_minute >= BURST_MAX or (role == "bidder" and total >= PRE_ASSIGN_MAX):
        return 429, {"error": "too_many_messages"}

    sender_role = "customer" if role == "customer" else "courier"
    # The other side: the customer, or the assigned courier. A customer's message before
    # assignment has no single recipient (the couriers who asked see it in their chat).
    recipient = order.customer_id if sender_role == "courier" else await courier_user_id(session, order)
    if recipient == user.id:
        recipient = None

    message = Message(
        order_id=order.id,
        sender_id=user.id,
        recipient_id=recipient,
        sender_role=sender_role,
        body=content,
        is_template=False,
    )
    session.add(message)
    await session.flush()
    emit(session, "Message", "create", message.id)

    notified = False
    if recipient is not None:
        preview = f"{content[:PREVIEW_PUSH]}…" if len(content) > PREVIEW_PUSH else content
        from_courier = sender_role == "courier"
        try:
            async with session.begin_nested():
                await notify(
                    session,
                    user_id=recipient,
                    order_id=order.id,
                    type_="new_message",
                    title_ar="💬 رسالة من المندوب" if from_courier else "💬 رسالة من العميل",
                    title_fr="💬 Message du livreur" if from_courier else "💬 Message du client",
                    body_ar=preview,
                    body_fr=preview,
                    metadata={
                        "recipient_role": "customer" if from_courier else "courier",
                        "sender_role": sender_role,
                        "message_id": str(message.id),
                        "message_preview": content[:PREVIEW_METADATA],
                        "timestamp": now_utc().isoformat().replace("+00:00", "Z"),
                    },
                )
            notified = True
        except Exception:
            # The recipient still sees the message (chat + unread polls).
            log.exception("new_message notification lost: order %s message %s", order.id, message.id)

    doc = (await _docs(session, messages.c.id == message.id, newest_first=True, limit=1))[0]
    return 200, {"success": True, "message": doc, "notified": notified}


# ─────────────────────────── getOrderMessages / markOrderMessagesRead ───────────────────────────


async def _order_and_role(
    session: AsyncSession, user: CurrentUser, payload: dict[str, Any]
) -> tuple[Order | None, str | None, Result | None]:
    order_id = str(payload.get("order_id") or "").strip()
    if not order_id:
        return None, None, (400, {"error": "Missing order_id"})
    order = await load_order(session, order_id)
    if order is None:
        return None, None, (404, {"error": "Order not found"})
    role = await chat_role(session, order, user)
    if role is None:
        return None, None, (403, {"error": "Forbidden"})
    return order, role, None


def _limit(raw: Any, default: int = 200) -> int:
    try:
        value = int(float(raw)) if raw not in (None, "", 0, False) else default
    except (TypeError, ValueError):
        value = default
    return min(500, max(1, value))


async def _mark(session: AsyncSession, *where: Any) -> list[uuid.UUID]:
    ids = list(
        (
            await session.execute(
                update(Message).where(*where).values(read_at=now_utc()).returning(Message.id)
            )
        ).scalars()
    )
    for message_id in ids:
        emit(session, "Message", "update", message_id)
    return ids


async def get_order_messages(session: AsyncSession, user: CurrentUser, payload: dict[str, Any]) -> Result:
    """The latest `limit` (1..500, default 200) visible messages, oldest first; with
    `mark_read: true` the caller's unread incoming ones among them are marked read."""
    order, role, error = await _order_and_role(session, user, payload)
    if error:
        return error
    assert order is not None and role is not None
    latest = await _docs(
        session, messages.c.order_id == order.id, _visible(role, user),
        newest_first=True, limit=_limit(payload.get("limit")),
    )  # fmt: skip
    docs = list(reversed(latest))
    if payload.get("mark_read") is not True:
        return 200, {"success": True, "messages": docs}
    shown = [uuid.UUID(d["id"]) for d in docs]
    marked = {
        str(i)
        for i in await _mark(session, messages.c.id.in_(shown), _incoming_unread(role, user))
    } if shown else set()  # fmt: skip
    return 200, {
        "success": True,
        "messages": [{**d, "is_read": True} if d["id"] in marked else d for d in docs],
        "marked": len(marked),
    }


async def mark_order_messages_read(
    session: AsyncSession, user: CurrentUser, payload: dict[str, Any]
) -> Result:
    order, role, error = await _order_and_role(session, user, payload)
    if error:
        return error
    assert order is not None and role is not None
    ids = await _mark(
        session, messages.c.order_id == order.id, _visible(role, user), _incoming_unread(role, user)
    )
    return 200, {"success": True, "marked": len(ids)}


# ─────────────────────────── listMyUnreadMessages ───────────────────────────


async def _fast_unread(session: AsyncSession, user: CurrentUser) -> list[dict[str, Any]]:
    """Messages addressed to the caller (recipient_id), on orders he really is a party of,
    from the other party (the customer, or the order's courier / a courier before assignment)."""
    o = Order.__table__.alias("unread_order")
    c = Courier.__table__.alias("unread_courier")
    party_and_sender = (
        exists()
        .select_from(o.outerjoin(c, c.c.id == o.c.courier_id))
        .where(
            o.c.id == messages.c.order_id,
            or_(o.c.customer_id == user.id, c.c.user_id == user.id),
            or_(
                and_(
                    o.c.customer_id == user.id,
                    messages.c.sender_role == "courier",
                    or_(c.c.user_id.is_(None), messages.c.sender_id == c.c.user_id),
                ),
                and_(o.c.customer_id != user.id, messages.c.sender_id == o.c.customer_id),
            ),
        )
    )
    return await _docs(
        session,
        messages.c.recipient_id == user.id,
        messages.c.read_at.is_(None),
        messages.c.sender_id.is_distinct_from(user.id),
        party_and_sender,
        newest_first=True,
        limit=MAX_UNREAD,
    )


async def _latest_order_ids(session: AsyncSession, *where: Any) -> list[uuid.UUID]:
    stmt = select(Order.id).where(*where).order_by(Order.updated_at.desc()).limit(ORDERS_PER_SIDE)
    return list((await session.execute(stmt)).scalars())


async def _full_unread(session: AsyncSession, user: CurrentUser) -> list[dict[str, Any]]:
    """Unread messages of the caller's own orders (customer / assigned courier: any message
    not his; open orders he asked about as a courier: the customer's messages only)."""
    other: set[uuid.UUID] = set(await _latest_order_ids(session, Order.customer_id == user.id))
    my_courier = (
        await session.execute(select(Courier.id).where(Courier.user_id == user.id))
    ).scalar_one_or_none()
    customer_only: set[uuid.UUID] = set()
    if my_courier is not None:
        other |= set(await _latest_order_ids(session, Order.courier_id == my_courier))
        sent = (
            select(messages.c.order_id)
            .where(messages.c.sender_id == user.id)
            .order_by(messages.c.created_at.desc())
            .limit(50)
            .subquery()
        )
        asked = (
            await session.execute(
                select(Order.id).where(
                    Order.id.in_(select(sent.c.order_id)),
                    Order.status.in_(OPEN_STATUSES),
                    Order.courier_id.is_(None),
                )
            )
        ).scalars()
        customer_only = set(asked) - other
    if not other and not customer_only:
        return []
    scope = []
    if other:
        scope.append(messages.c.order_id.in_(other))
    if customer_only:
        scope.append(and_(messages.c.order_id.in_(customer_only), messages.c.sender_role == "customer"))
    return await _docs(
        session,
        or_(*scope),
        messages.c.read_at.is_(None),
        messages.c.sender_id.is_distinct_from(user.id),
        newest_first=True,
        limit=MAX_UNREAD,
    )


async def list_my_unread_messages(
    session: AsyncSession, user: CurrentUser, payload: dict[str, Any]
) -> Result:
    mode = "fast" if payload.get("mode") == "fast" else "full"
    found = await (_fast_unread if mode == "fast" else _full_unread)(session, user)
    return 200, {"success": True, "messages": found, "mode": mode}
