"""In-app notification + push fan-out, the port of sendNotificationIfEnabled's core.

Every domain service notifies through `notify` (or `notify_detailed` to know what
happened to the push). The in-app row is always written; the user's preferences only
govern the push. The caller owns the transaction: the row, its
realtime event, the push log and a WhatsApp fallback row commit together.

WhatsApp instead of push (sendNotificationIfEnabled.whatsappInsteadOfPush): a customer
without any active device (web only, notifications refused) who ticked the WhatsApp
opt-in gets `on_the_way` / `new_offer` of an order by WhatsApp. Template parameters come
from the database, never from the caller's text; one message per order and step
(idempotency key); only when WhatsApp is configured (settings.whatsapp_enabled).
"""

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Courier, OrderOffer, OrderStop
from app.models.identity import User
from app.models.notifications import NOTIFICATION_TYPE_SYNONYMS, NOTIFICATION_TYPES, Notification
from app.models.orders import Order
from app.realtime.events import emit
from app.security.tokens import now_utc
from app.services import order_texts, whatsapp
from app.services.push import PushMessage, send_to_user

log = logging.getLogger("odsd.notifications")

TITLE_MAX = 200
BODY_MAX = 1000

# sendNotificationIfEnabled.PREFERENCE_KEY: the legacy preference key governing the push of
# a type, keyed by the type AS SENT (synonyms included: `incoming_order` has its own key).
# Types absent from it (emergency_contact, account_verified...) are always pushed.
LEGACY_PREFERENCE_KEY: dict[str, str] = {
    **{
        t: "order_status_changes"
        for t in (
            "order_confirmed", "order_accepted", "at_shop", "purchased", "on_the_way", "delivered",
            "order_delivered", "order_cancelled", "courier_on_way", "eta_update", "delivery_delayed",
            "order_preparing", "new_offer",
        )
    },
    "new_order": "new_orders",
    "incoming_order": "incoming_orders",
    "message": "chat_messages",
    "new_message": "chat_messages",
}  # fmt: skip
# legacy notification_preferences key -> users column (same as the UserProfile compat entity)
PREFERENCE_KEY_COLUMN = {
    "order_status_changes": "notify_order_status",
    "new_orders": "notify_new_orders",
    "incoming_orders": "notify_incoming_orders",
    "chat_messages": "notify_chat",
}
# Preference column of each STORED type (kept for callers that import it).
PREFERENCE_COLUMN: dict[str, str | None] = {
    t: PREFERENCE_KEY_COLUMN[k] for t, k in LEGACY_PREFERENCE_KEY.items() if t in NOTIFICATION_TYPES
}

# An order notice already written for the same (user, order, type) this recently is not written
# again: the server sends the order steps itself and the installed apps still send them after
# the call (sendNotificationIfEnabled answers {skipped: "duplicate"}).
DEDUPE_WINDOW = timedelta(seconds=120)

WHATSAPP_FALLBACK_TYPES = {"on_the_way", "new_offer"}  # stored types (courier_on_way → on_the_way)
TEST_ORDER = re.compile(r"QA TEST|\bPW-", re.IGNORECASE)


@dataclass(frozen=True)
class NotifyResult:
    notification: Notification
    # None when the push was attempted; else 'push_disabled' / '<preference key>_disabled'.
    push_skipped: str | None
    push: dict[str, Any] | None = None  # send_to_user's summary
    whatsapp: dict[str, Any] | None = None  # the WhatsApp fallback's answer, when it ran


def canonical_type(type_: str) -> str:
    """Maps legacy synonyms (`message`, `order_delivered`…) to the stored type; rejects unknown."""
    stored = NOTIFICATION_TYPE_SYNONYMS.get(type_, type_)
    if stored not in NOTIFICATION_TYPES:
        raise ValueError(f"unknown notification type: {type_}")
    return stored


def _cap(value: str | None, limit: int) -> str | None:
    return value[:limit] if value else value


def push_skip_reason(user: User | None, type_as_sent: str) -> str | None:
    """Why the push is not sent (the in-app row is written anyway), None if it is."""
    if user is None or user.deleted_at is not None or not user.push_enabled:
        return "push_disabled"
    key = LEGACY_PREFERENCE_KEY.get(type_as_sent) or LEGACY_PREFERENCE_KEY.get(canonical_type(type_as_sent))
    if key and not getattr(user, PREFERENCE_KEY_COLUMN[key]):
        return f"{key}_disabled"
    return None


async def recent_duplicate(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    order_id: uuid.UUID | None,
    type_: str,
    offer_id: str | None = None,
) -> Notification | None:
    """The notification of the same (user, order, type) written in the last DEDUPE_WINDOW, if any.
    `type_` may be a legacy synonym. For `new_offer`, `offer_id` narrows it to that offer (two
    couriers' offers a minute apart are two notices). Notices without an order are never duplicates."""
    if order_id is None:
        return None
    stored = canonical_type(type_)
    stmt = select(Notification).where(
        Notification.user_id == user_id,
        Notification.order_id == order_id,
        Notification.type == stored,
        Notification.created_at >= now_utc() - DEDUPE_WINDOW,
    )
    if offer_id:
        stmt = stmt.where(Notification.data["offer_id"].astext == str(offer_id))
    return (
        await session.execute(stmt.order_by(Notification.created_at.desc()).limit(1))
    ).scalar_one_or_none()


async def notify(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    type_: str,
    title_ar: str,
    title_fr: str,
    body_ar: str = "",
    body_fr: str = "",
    order_id: uuid.UUID | None = None,
    metadata: dict[str, Any] | None = None,
    push: bool = True,
) -> Notification:
    """Writes the notification, queues its realtime event and pushes it when the
    user's preferences allow. Returns the flushed row."""
    result = await notify_detailed(
        session, user_id=user_id, type_=type_, title_ar=title_ar, title_fr=title_fr, body_ar=body_ar,
        body_fr=body_fr, order_id=order_id, metadata=metadata, push=push,
    )  # fmt: skip
    return result.notification


async def notify_detailed(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    type_: str,
    title_ar: str,
    title_fr: str,
    body_ar: str = "",
    body_fr: str = "",
    order_id: uuid.UUID | None = None,
    metadata: dict[str, Any] | None = None,
    push: bool = True,
) -> NotifyResult:
    """`notify`, answering what happened to the push (sendNotificationIfEnabled's `push_skipped`)."""
    stored_type = canonical_type(type_)
    row = Notification(
        user_id=user_id,
        order_id=order_id,
        type=stored_type,
        title_ar=_cap(title_ar, TITLE_MAX),
        title_fr=_cap(title_fr, TITLE_MAX),
        body_ar=_cap(body_ar, BODY_MAX),
        body_fr=_cap(body_fr, BODY_MAX),
        data=metadata or {},
    )
    session.add(row)
    await session.flush()
    emit(session, "Notification", "create", row.id)
    if not push:
        return NotifyResult(row, push_skipped=None)
    user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    skipped = push_skip_reason(user, type_)
    if skipped or user is None:
        return NotifyResult(row, push_skipped=skipped)
    summary = await send_to_user(
        session,
        user_id,
        PushMessage(
            type=stored_type,
            title_ar=row.title_ar or "",
            title_fr=row.title_fr or "",
            body_ar=row.body_ar or "",
            body_fr=row.body_fr or "",
            order_id=str(order_id) if order_id else None,
            notification_id=str(row.id),
            metadata=row.data,
        ),
    )
    whatsapp = None
    if (
        summary.get("attempted") == 0
        and order_id is not None
        and stored_type in WHATSAPP_FALLBACK_TYPES
        and user.whatsapp_opt_in_at is not None
        and settings.whatsapp_enabled
    ):
        whatsapp = await _whatsapp_instead_of_push(session, stored_type, order_id, user, row.id)
    return NotifyResult(row, push_skipped=None, push=summary, whatsapp=whatsapp)


async def _whatsapp_instead_of_push(
    session: AsyncSession, stored_type: str, order_id: uuid.UUID, user: User, notification_id: uuid.UUID
) -> dict[str, Any] | None:
    try:
        async with session.begin_nested():
            order = await session.get(Order, order_id)
            if order is None or order.customer_id != user.id or TEST_ORDER.search(order.items_text or ""):
                return None
            first_stop = (
                await session.execute(
                    select(OrderStop.name)
                    .where(OrderStop.order_id == order_id)
                    .order_by(OrderStop.seq)
                    .limit(1)
                )
            ).scalar_one_or_none()
            label = str(first_stop or order.items_text or "ODS")[:40]
            if stored_type == "new_offer":
                fee = (
                    await session.execute(
                        select(OrderOffer.proposed_fee)
                        .where(OrderOffer.order_id == order_id)
                        .order_by(OrderOffer.created_at.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                template, params = "new_offer", [label, f"{fee:.3f}" if fee is not None else "-"]
                key = f"offer:{order_id}"
            else:
                courier_name = None
                if order.courier_id:
                    courier_name = (
                        await session.execute(
                            select(Courier.display_name).where(Courier.id == order.courier_id)
                        )
                    ).scalar_one_or_none()
                shown = order_texts.short_name(courier_name) or "ODS"  # one way to write the name (R30)
                template, params = "courier_on_the_way", [shown, label]
                key = f"ontheway:{order_id}"
            _status, body = await whatsapp.send_template(
                session,
                template_key=template,
                params=params,
                idempotency_key=key,
                user_id=user.id,
                order_id=order_id,
                notification_id=notification_id,
            )
            return body
    except Exception:
        log.exception("WhatsApp fallback failed for order %s", order_id)
        return None


# ─────────────────────────── markNotificationsRead ───────────────────────────

MARK_READ_MAX = 500


async def retire_acceptance_notices(session: AsyncSession, order_id: uuid.UUID, courier_user_id: Any) -> int:
    """The courier lost this order (customer blocked him then chose someone else, cancelled, order
    expired): his unread « 🎉 Offre acceptée » notices for it are marked read, so that no app
    shows « Allez au magasin » for an order that is no longer his (QA 06/10, R1)."""
    if order_id is None or courier_user_id is None:
        return 0
    retired = list(
        (
            await session.execute(
                update(Notification)
                .where(
                    Notification.order_id == order_id,
                    Notification.user_id == courier_user_id,
                    Notification.type == "order_accepted",
                    Notification.read_at.is_(None),
                )
                .values(read_at=now_utc())
                .returning(Notification.id)
            )
        ).scalars()
    )
    for notice_id in retired:
        emit(session, "Notification", "update", notice_id)
    return len(retired)


async def mark_read(session: AsyncSession, user: Any, raw_ids: Any) -> tuple[int, dict[str, Any]]:
    """The caller's own unread rows among `raw_ids` (1..500) marked read in one statement (QA 06/10
    B59: the Notifications page sent one request per row). Junk / foreign ids are ignored."""
    if not isinstance(raw_ids, list) or not 0 < len(raw_ids) <= MARK_READ_MAX:
        return 400, {"error": "invalid_ids", "max": MARK_READ_MAX}
    ids: set[uuid.UUID] = set()
    for value in raw_ids:
        try:
            ids.add(uuid.UUID(str(value)))
        except ValueError:
            continue
    if not ids:
        return 200, {"success": True, "marked": 0}
    marked = list(
        (
            await session.execute(
                update(Notification)
                .where(
                    Notification.id.in_(ids), Notification.user_id == user.id, Notification.read_at.is_(None)
                )
                .values(read_at=now_utc())
                .returning(Notification.id)
            )
        ).scalars()
    )
    for notice_id in marked:
        emit(session, "Notification", "update", notice_id)
    return 200, {"success": True, "marked": len(marked)}
