"""Notices the order flows send whatever the user's preferences.

cancelOrder and expireStaleOrders wrote their Notification rows and called sendPushToTokens
directly: no preference gate (a cancellation must reach the phone). `notify` writes the row
and its realtime event; the push is sent here without the preference check.
"""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Notification, User
from app.services.notifications import notify
from app.services.push import PushMessage, send_to_user


async def notify_always_pushed(session: AsyncSession, *, user_id: uuid.UUID, **kwargs: Any) -> Notification:
    row = await notify(session, user_id=user_id, push=False, **kwargs)
    user = await session.get(User, user_id)
    if user is not None and user.deleted_at is None:
        await send_to_user(
            session,
            user_id,
            PushMessage(
                type=row.type,
                title_ar=row.title_ar or "",
                title_fr=row.title_fr or "",
                body_ar=row.body_ar or "",
                body_fr=row.body_fr or "",
                order_id=str(row.order_id) if row.order_id else None,
                notification_id=str(row.id),
                metadata=row.data,
            ),
        )
    return row
