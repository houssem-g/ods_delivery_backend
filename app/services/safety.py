"""Blocking and reporting users (App Review 1.2: the order chat is user-generated content).

A block works BOTH ways, whoever made it:
  - chat: neither can write to the other; messages of a blocked user are hidden from the blocker
    and vice versa (getOrderMessages, unread lists);
  - orders: a blocked courier no longer sees the customer's open orders, gets no new_order notice
    and cannot bid; the customer no longer sees his offers and cannot accept them.
An order already running goes on (the delivery is not dropped), only the chat stops.

A report is kept in user_reports and every admin gets an in-app notice (user_reported); the
admin resolves it (and may disable the account from the Utilisateurs tab).

Every function answers `(status, json)` like the other functions.
"""

import uuid
from typing import Any

from sqlalchemy import and_, delete, exists, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Courier, Message, Order, OrderOffer, OrderStop, User, UserBlock, UserReport
from app.models.safety import REPORT_REASONS
from app.realtime.events import emit
from app.security.deps import CurrentUser
from app.security.tokens import now_utc
from app.services.notifications import notify

Result = tuple[int, dict[str, Any]]
MAX_DETAILS = 1000
MAX_EXCERPT = 500
REPORTS_PER_DAY = 20
blocks = UserBlock.__table__


def _as_uuid(raw: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(raw))
    except ValueError:
        return None


def _first_name(full_name: str | None) -> str:
    return (full_name or "").strip().split(" ")[0]


# ─────────────────────────── checks used by the other services ───────────────────────────


def blocked_pair(a: Any, b: Any) -> Any:
    """SQL: a block exists between the users a and b (columns or values), in either direction."""
    return exists().where(
        or_(
            and_(blocks.c.blocker_id == a, blocks.c.blocked_id == b),
            and_(blocks.c.blocker_id == b, blocks.c.blocked_id == a),
        )
    )


async def is_blocked(session: AsyncSession, a: uuid.UUID | None, b: uuid.UUID | None) -> bool:
    if a is None or b is None or a == b:
        return False
    return bool((await session.execute(select(blocked_pair(a, b)))).scalar())


async def block_direction(session: AsyncSession, me: uuid.UUID, other: uuid.UUID | None) -> str | None:
    """'me' when I blocked the other person (also when both did: I can unblock), 'them' when only
    they blocked me, None without a block. The app words its banner after it."""
    if other is None or other == me:
        return None
    rows = (
        (
            await session.execute(
                select(blocks.c.blocker_id).where(
                    or_(
                        and_(blocks.c.blocker_id == me, blocks.c.blocked_id == other),
                        and_(blocks.c.blocker_id == other, blocks.c.blocked_id == me),
                    )
                )
            )
        )
        .scalars()
        .all()
    )
    if me in rows:
        return "me"
    return "them" if rows else None


async def blocked_with(session: AsyncSession, user_id: uuid.UUID) -> set[uuid.UUID]:
    """Every user in a block with `user_id`, whoever blocked whom."""
    rows = await session.execute(
        select(blocks.c.blocker_id, blocks.c.blocked_id).where(
            or_(blocks.c.blocker_id == user_id, blocks.c.blocked_id == user_id)
        )
    )
    return {b if a == user_id else a for a, b in rows}


# ─────────────────────────── who is "the other person" ───────────────────────────


async def _courier_user(session: AsyncSession, courier_id: uuid.UUID | None) -> uuid.UUID | None:
    if courier_id is None:
        return None
    return (
        await session.execute(select(Courier.user_id).where(Courier.id == courier_id))
    ).scalar_one_or_none()


async def _target(
    session: AsyncSession, user: CurrentUser, payload: dict[str, Any]
) -> tuple[uuid.UUID | None, Order | None, Message | None, Result | None]:
    """The user to block / report, resolved server-side and only among people the caller dealt
    with: the sender of a message of one of his chats (`message_id`), the other party of one of
    his orders (`order_id`: customer ↔ assigned courier), or the courier of an offer he received
    (`offer_courier_id` + `order_id`)."""
    message: Message | None = None
    order: Order | None = None
    if payload.get("message_id"):
        message_id = _as_uuid(payload.get("message_id"))
        message = await session.get(Message, message_id) if message_id else None
        if message is None:
            return None, None, None, (404, {"error": "message_not_found"})
        order = await session.get(Order, message.order_id)
    elif payload.get("order_id"):
        order_id = _as_uuid(payload.get("order_id"))
        order = await session.get(Order, order_id) if order_id else None
    else:
        return None, None, None, (400, {"error": "Missing order_id"})
    if order is None:
        return None, None, None, (404, {"error": "order_not_found"})

    courier_user = await _courier_user(session, order.courier_id)
    is_customer = order.customer_id == user.id
    is_courier = courier_user == user.id

    async def wrote_on_order() -> bool:
        return bool(
            (
                await session.execute(
                    select(exists().where(Message.order_id == order.id, Message.sender_id == user.id))
                )
            ).scalar()
        )

    if message is not None:
        # the customer: any message of his order (the courier's, or a bidder's before assignment);
        # a courier: the customer's messages of an order he delivers, received or asked about
        from_customer = message.sender_id == order.customer_id
        allowed = is_customer or (
            from_customer and (is_courier or message.recipient_id == user.id or await wrote_on_order())
        )
        if not allowed:
            return None, None, None, (403, {"error": "not_a_party"})
        target = message.sender_id
    elif payload.get("offer_courier_id"):
        if not is_customer:
            return None, None, None, (403, {"error": "not_a_party"})
        offer_courier = _as_uuid(payload.get("offer_courier_id"))
        made_offer = (
            offer_courier is not None
            and (
                await session.execute(
                    select(
                        exists().where(
                            OrderOffer.order_id == order.id, OrderOffer.courier_id == offer_courier
                        )
                    )
                )
            ).scalar()
        )
        if not made_offer:
            return None, None, None, (404, {"error": "offer_not_found"})
        target = await _courier_user(session, offer_courier)
    elif is_customer:
        target = courier_user
    elif is_courier or await wrote_on_order():
        # the assigned courier, or a courier who asked a question before assignment
        target = order.customer_id
    else:
        return None, None, None, (403, {"error": "not_a_party"})
    if target is None:
        return None, None, None, (409, {"error": "no_other_party"})
    if target == user.id:
        return None, None, None, (400, {"error": "cannot_target_self"})
    return target, order, message, None


# ─────────────────────────── blockUser / unblockUser / listBlockedUsers ───────────────────────────

LIVE = ("accepted", "at_shop", "price_confirmation_needed", "purchased", "on_the_way", "client_no_response")
RELEASABLE = ("accepted", "at_shop", "price_confirmation_needed")  # = cancellation.RELEASABLE_AFTER_BLOCK


async def _live_order(
    session: AsyncSession, user: CurrentUser, order: Order | None, target: uuid.UUID
) -> dict[str, Any] | None:
    """The customer just blocked the courier of this running order: may he still give it to someone
    else (before the purchase: releaseBlockedCourier)? None in every other case."""
    if order is None or order.customer_id != user.id or order.status not in LIVE:
        return None
    if await _courier_user(session, order.courier_id) != target:
        return None
    return {"id": str(order.id), "status": order.status, "releasable": order.status in RELEASABLE}


async def _block(session: AsyncSession, blocker: uuid.UUID, blocked: uuid.UUID) -> bool:
    """True when the block is new."""
    inserted = await session.execute(
        insert(UserBlock)
        .values(blocker_id=blocker, blocked_id=blocked)
        .on_conflict_do_nothing(index_elements=["blocker_id", "blocked_id"])
        .returning(UserBlock.id)
    )
    return inserted.first() is not None


def _signal(session: AsyncSession, *users: uuid.UUID) -> None:
    """Open screens of both users refresh (chat, offers, open orders)."""
    for user_id in users:
        emit(session, "UserProfile", "update", user_id)


async def block_user(session: AsyncSession, user: CurrentUser, payload: dict[str, Any]) -> Result:
    target, order, _, error = await _target(session, user, payload)
    if error:
        return error
    assert target is not None
    created = await _block(session, user.id, target)
    _signal(session, user.id, target)
    return 200, {
        "success": True,
        "blocked_user_id": str(target),
        "already_blocked": not created,
        "live_order": await _live_order(session, user, order, target),
    }


async def unblock_user(session: AsyncSession, user: CurrentUser, payload: dict[str, Any]) -> Result:
    target = _as_uuid(payload.get("user_id")) if payload.get("user_id") else None
    if target is None:
        return 400, {"error": "Missing user_id"}
    removed = await session.execute(
        delete(UserBlock).where(UserBlock.blocker_id == user.id, UserBlock.blocked_id == target)
    )
    if not removed.rowcount:
        return 404, {"error": "not_blocked"}
    _signal(session, user.id, target)
    return 200, {"success": True}


async def _last_shared_order(
    session: AsyncSession, me: uuid.UUID, other: uuid.UUID
) -> tuple[str, str | None, Any] | None:
    """The latest order where the caller met `other`: (the other's role there 'customer' /
    'courier', the shop's name, the order's date), None when there is none. One account can be
    both customer and courier: the role comes from the order, not from the profile (QA B54)."""
    my_couriers = select(Courier.id).where(Courier.user_id == me)
    their_couriers = select(Courier.id).where(Courier.user_id == other)
    met = or_(
        and_(Order.customer_id == me, Order.courier_id.in_(their_couriers)),
        and_(Order.customer_id == other, Order.courier_id.in_(my_couriers)),
        and_(
            Order.customer_id == me,
            exists().where(OrderOffer.order_id == Order.id, OrderOffer.courier_id.in_(their_couriers)),
        ),
        and_(
            Order.customer_id == other,
            exists().where(Message.order_id == Order.id, Message.sender_id == me),
        ),
    )
    shop = (
        select(OrderStop.name)
        .where(OrderStop.order_id == Order.id)
        .order_by(OrderStop.seq)
        .limit(1)
        .scalar_subquery()
    )
    row = (
        await session.execute(
            select(Order.customer_id, shop, Order.created_at)
            .where(met)
            .order_by(Order.created_at.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    customer_id, shop_name, created = row
    return ("customer" if customer_id == other else "courier"), shop_name, created


def _iso_z(value: Any) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value else None


async def list_blocked_users(session: AsyncSession, user: CurrentUser, payload: dict[str, Any]) -> Result:
    """The people the caller blocked (first name only), newest first, each with the role he met
    them in (`role`: 'customer' / 'courier') and the shop + date of that order, so two people
    with the same first name can be told apart."""
    rows = (
        await session.execute(
            select(UserBlock.blocked_id, UserBlock.created_at, User.full_name, Courier.display_name)
            .join(User, User.id == UserBlock.blocked_id)
            .outerjoin(Courier, Courier.user_id == UserBlock.blocked_id)
            .where(UserBlock.blocker_id == user.id)
            .order_by(UserBlock.created_at.desc())
            .limit(200)
        )
    ).all()
    blocked = []
    for blocked_id, created, full_name, display_name in rows:
        shared = await _last_shared_order(session, user.id, blocked_id)
        role = shared[0] if shared else ("courier" if display_name is not None else "customer")
        # the name he goes by in that role: a customer's own name, a courier's display name
        name = (full_name or display_name) if role == "customer" else (display_name or full_name)
        blocked.append(
            {
                "user_id": str(blocked_id),
                "first_name": _first_name(name) or "—",
                "role": role,
                "is_courier": role == "courier",
                "shop_name": shared[1] if shared else None,
                "order_date": _iso_z(shared[2]) if shared else None,
                "blocked_at": _iso_z(created),
            }
        )
    return 200, {"success": True, "blocked": blocked}


# ─────────────────────────── reportUser ───────────────────────────


REASON_LABELS = {
    "harassment": ("Harcèlement ou insultes", "تحرش أو شتائم"),
    "inappropriate": ("Contenu inapproprié", "محتوى غير لائق"),
    "spam": ("Spam ou publicité", "رسائل مزعجة أو إشهار"),
    "fraud": ("Arnaque ou fraude", "احتيال"),
    "dangerous": ("Comportement dangereux", "سلوك خطير"),
    "other": ("Autre", "أخرى"),
}


async def report_user(session: AsyncSession, user: CurrentUser, payload: dict[str, Any]) -> Result:
    """Body { order_id | message_id (| order_id + offer_courier_id), reason, details?, block? }.
    `block: true` also blocks the person (one tap "Signaler et bloquer")."""
    reason = payload.get("reason")
    if reason not in REPORT_REASONS:
        return 400, {"error": "invalid_reason", "reasons": list(REPORT_REASONS)}
    raw_details = payload.get("details")
    details = raw_details.strip()[:MAX_DETAILS] if isinstance(raw_details, str) else ""
    target, order, message, error = await _target(session, user, payload)
    if error:
        return error
    assert target is not None and order is not None
    since = now_utc().replace(hour=0, minute=0, second=0, microsecond=0)
    today = (
        await session.execute(
            select(UserReport.id).where(UserReport.reporter_id == user.id, UserReport.created_at >= since)
        )
    ).all()
    if len(today) >= REPORTS_PER_DAY:
        return 429, {"error": "too_many_reports"}

    report = UserReport(
        reporter_id=user.id,
        reported_id=target,
        order_id=order.id,
        message_id=message.id if message else None,
        message_excerpt=(message.body or "")[:MAX_EXCERPT] if message else None,
        reason=reason,
        details=details or None,
    )
    session.add(report)
    await session.flush()
    blocked = False
    if payload.get("block") is True:
        await _block(session, user.id, target)
        _signal(session, user.id, target)
        blocked = True

    label_fr, label_ar = REASON_LABELS[reason]
    ref = str(order.id)[-6:].upper()
    admins = (
        await session.execute(
            select(User.id).where(User.role == "admin", User.deleted_at.is_(None), User.id != target)
        )
    ).scalars()
    for admin_id in admins:
        await notify(
            session,
            user_id=admin_id,
            type_="user_reported",
            order_id=order.id,
            push=False,
            title_fr="🚩 Signalement d'un utilisateur",
            title_ar="🚩 تبليغ عن مستخدم",
            body_fr=f"{label_fr} — commande #{ref}. À traiter dans Admin › Signalements.",
            body_ar=f"{label_ar} — الطلب #{ref}. للمعالجة في الإدارة › التبليغات.",
            metadata={"report_id": str(report.id), "reason": reason, "recipient_role": "admin"},
        )
    return 200, {
        "success": True,
        "report_id": str(report.id),
        "blocked": blocked,
        "live_order": await _live_order(session, user, order, target) if blocked else None,
    }


# ─────────────────────────── admin: listUserReports / resolveUserReport ───────────────────────────


async def list_user_reports(session: AsyncSession, user: CurrentUser, payload: dict[str, Any]) -> Result:
    if not user.is_admin:
        return 403, {"error": "Forbidden"}
    status = payload.get("status") or "open"
    if status not in ("open", "resolved", "dismissed", "all"):
        return 400, {"error": "invalid_status"}
    reporter = User.__table__.alias("reporter")
    reported = User.__table__.alias("reported")
    stmt = (
        select(
            UserReport,
            reporter.c.full_name.label("reporter_name"),
            reporter.c.email.label("reporter_email"),
            reported.c.full_name.label("reported_name"),
            reported.c.email.label("reported_email"),
            reported.c.disabled_at.label("reported_disabled_at"),
        )
        .outerjoin(reporter, reporter.c.id == UserReport.reporter_id)
        .outerjoin(reported, reported.c.id == UserReport.reported_id)
        .order_by(UserReport.created_at.desc())
        .limit(200)
    )
    if status != "all":
        stmt = stmt.where(UserReport.status == status)
    out = []
    for row in await session.execute(stmt):
        r: UserReport = row[0]
        reported_count = (
            await session.execute(select(UserReport.id).where(UserReport.reported_id == r.reported_id))
        ).all() if r.reported_id else []  # fmt: skip
        out.append(
            {
                "id": str(r.id),
                "reason": r.reason,
                "details": r.details,
                "message_excerpt": r.message_excerpt,
                "status": r.status,
                "order_id": str(r.order_id) if r.order_id else None,
                "created_date": r.created_at.isoformat().replace("+00:00", "Z"),
                "reporter": {"id": str(r.reporter_id) if r.reporter_id else None,
                             "name": row.reporter_name, "email": row.reporter_email},
                "reported": {"id": str(r.reported_id) if r.reported_id else None,
                             "name": row.reported_name, "email": row.reported_email,
                             "is_active": row.reported_disabled_at is None,
                             "reports_total": len(reported_count)},
            }
        )  # fmt: skip
    return 200, {"success": True, "reports": out}


async def resolve_user_report(session: AsyncSession, user: CurrentUser, payload: dict[str, Any]) -> Result:
    """Body { report_id, status: 'resolved' | 'dismissed' | 'open', disable_user?: bool }."""
    if not user.is_admin:
        return 403, {"error": "Forbidden"}
    report_id = _as_uuid(payload.get("report_id")) if payload.get("report_id") else None
    report = await session.get(UserReport, report_id, with_for_update=True) if report_id else None
    if report is None:
        return 404, {"error": "report_not_found"}
    status = payload.get("status")
    if status not in ("resolved", "dismissed", "open"):
        return 400, {"error": "invalid_status"}
    report.status = status
    report.resolved_at = None if status == "open" else now_utc()
    report.resolved_by = None if status == "open" else user.id
    disabled = False
    if payload.get("disable_user") is True and report.reported_id is not None:
        target = await session.get(User, report.reported_id, with_for_update=True)
        if target is not None and target.role != "admin" and target.disabled_at is None:
            target.disabled_at = now_utc()
            emit(session, "UserProfile", "update", target.id)
            disabled = True
    await session.flush()
    return 200, {"success": True, "status": status, "user_disabled": disabled}
