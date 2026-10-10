"""Notification: in-app notifications (`notifications`).

- read: the recipient (`user_id` = his e-mail) and admins;
- create: an admin, for anyone (AdminDashboard writes `account_verified` /
  `account_rejected` after a courier verification; the row is pushed too), or the
  recipient himself (the orderFlow fallback when sendNotificationIfEnabled
  did not answer a `notification_id` — writing into somebody else's list is refused);
- update: the recipient only, and only `is_read` (→ `read_at`), as
  roleNotifications.markNotificationsRead does;
- delete: the recipient only (Aurora: swipe to delete); an admin reading someone else's gets 403.
The other writers are the domain services, through app/services/notifications.notify.
"""

import json
import uuid
from typing import Any

from sqlalchemy import select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.compat.payload import coerce_payload
from app.compat.registry import EntityDef, LegacyField, register
from app.errors import ApiError
from app.models import Notification, Order, User
from app.security.deps import CurrentUser
from app.security.tokens import now_utc
from app.services.notifications import canonical_type, notify

notifications = Notification.__table__
recipient = User.__table__.alias("notification_user")

CREATE_FIELDS = frozenset(
    {"user_id", "order_id", "type", "title_ar", "title_fr", "body_ar", "body_fr", "metadata"}
)
METADATA_MAX = 2000  # bytes of JSON, same cap as sendNotificationIfEnabled


def _read_policy(user: CurrentUser) -> Any:
    return true() if user.is_admin else notifications.c.user_id == user.id


def _denied(operation: str) -> ApiError:
    return ApiError(403, "permission_denied", f"Permission denied for {operation} operation on Notification")


async def create(session: AsyncSession, actor: CurrentUser, data: dict[str, Any]) -> str:
    values = coerce_payload(ENTITY, data, CREATE_FIELDS)
    target_email = (values.get("user_id") or "").strip()
    if not target_email:
        raise ApiError(400, "validation_error", "user_id: required")
    if not actor.is_admin and target_email.lower() != actor.email.lower():
        raise _denied("create")
    target = (
        await session.execute(select(User).where(User.email == target_email, User.deleted_at.is_(None)))
    ).scalar_one_or_none()
    if target is None:
        raise ApiError(400, "validation_error", "user_id: unknown user")
    try:
        type_ = canonical_type(str(values.get("type") or ""))
    except ValueError as exc:
        raise ApiError(400, "validation_error", "type: unknown notification type") from exc
    order_id = values.get("order_id")
    if order_id is not None and await session.get(Order, order_id) is None:
        raise ApiError(400, "validation_error", "order_id: unknown order")
    metadata = values.get("metadata") or {}
    if len(_json(metadata)) > METADATA_MAX:
        raise ApiError(400, "validation_error", "metadata: too large")
    row = await notify(
        session,
        user_id=target.id,
        type_=type_,
        title_ar=values.get("title_ar") or "",
        title_fr=values.get("title_fr") or "",
        body_ar=values.get("body_ar") or "",
        body_fr=values.get("body_fr") or "",
        order_id=order_id,
        metadata=metadata,
        push=actor.is_admin,
    )
    return str(row.id)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


async def update(session: AsyncSession, actor: CurrentUser, doc_id: str, data: dict[str, Any]) -> None:
    try:
        target = uuid.UUID(doc_id)
    except ValueError as exc:
        raise ApiError(404, "not_found", "Notification not found") from exc
    row = await session.get(Notification, target, with_for_update=True)
    if row is None or (row.user_id != actor.id and not actor.is_admin):
        raise ApiError(404, "not_found", "Notification not found")
    if row.user_id != actor.id:
        raise _denied("update")
    values = coerce_payload(ENTITY, data, {"is_read"})
    if "is_read" in values:
        if values["is_read"]:
            row.read_at = row.read_at or now_utc()
        else:
            row.read_at = None
    await session.flush()


async def delete(session: AsyncSession, actor: CurrentUser, doc_id: str) -> list[uuid.UUID]:
    try:
        target = uuid.UUID(doc_id)
    except ValueError as exc:
        raise ApiError(404, "not_found", "Notification not found") from exc
    row = await session.get(Notification, target, with_for_update=True)
    if row is None or (row.user_id != actor.id and not actor.is_admin):
        raise ApiError(404, "not_found", "Notification not found")
    if row.user_id != actor.id:
        raise _denied("delete")
    owner = row.user_id
    await session.delete(row)
    await session.flush()
    return [owner]


ENTITY = register(
    EntityDef(
        name="Notification",
        source=notifications.join(recipient, recipient.c.id == notifications.c.user_id),
        id_expr=notifications.c.id,
        id_type="uuid",
        created_expr=notifications.c.created_at,
        updated_expr=notifications.c.updated_at,
        fields={
            "user_id": LegacyField(recipient.c.email, "string"),
            "order_id": LegacyField(notifications.c.order_id, "id"),
            "type": LegacyField(notifications.c.type, "string"),
            "title_ar": LegacyField(notifications.c.title_ar, "string"),
            "title_fr": LegacyField(notifications.c.title_fr, "string"),
            "body_ar": LegacyField(notifications.c.body_ar, "string"),
            "body_fr": LegacyField(notifications.c.body_fr, "string"),
            "metadata": LegacyField(notifications.c.data, "object"),
            "is_read": LegacyField(notifications.c.read_at.is_not(None), "boolean"),
        },
        read_policy=_read_policy,
        create=create,
        update=update,
        delete=delete,
    )
)
