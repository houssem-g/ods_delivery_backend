"""In-app notification + push fan-out, the port of sendNotificationIfEnabled's core.

Every domain service notifies through `notify`. The in-app row is always written;
the user's preferences only govern the push (as on Base44). The caller owns the
transaction: the row, its realtime event and the push log commit together.
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.identity import User
from app.models.notifications import NOTIFICATION_TYPE_SYNONYMS, NOTIFICATION_TYPES, Notification
from app.realtime.events import emit
from app.services.push import PushMessage, send_to_user

TITLE_MAX = 200
BODY_MAX = 1000

# Preference column of `users` that governs the push of each type (None = always pushed).
PREFERENCE_COLUMN: dict[str, str | None] = {
    **{
        t: "notify_order_status"
        for t in (
            "order_confirmed", "order_accepted", "at_shop", "purchased", "on_the_way", "delivered",
            "order_cancelled", "eta_update", "delivery_delayed", "order_preparing", "new_offer",
        )
    },
    "new_order": "notify_new_orders",
    "new_message": "notify_chat",
}  # fmt: skip


def canonical_type(type_: str) -> str:
    """Maps legacy synonyms (`message`, `order_delivered`…) to the stored type; rejects unknown."""
    stored = NOTIFICATION_TYPE_SYNONYMS.get(type_, type_)
    if stored not in NOTIFICATION_TYPES:
        raise ValueError(f"unknown notification type: {type_}")
    return stored


def _cap(value: str | None, limit: int) -> str | None:
    return value[:limit] if value else value


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
    if push and await _push_allowed(session, user_id, stored_type):
        await send_to_user(
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
    return row


async def _push_allowed(session: AsyncSession, user_id: uuid.UUID, stored_type: str) -> bool:
    user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None or not user.push_enabled or user.deleted_at is not None:
        return False
    column = PREFERENCE_COLUMN.get(stored_type)
    return column is None or bool(getattr(user, column))
