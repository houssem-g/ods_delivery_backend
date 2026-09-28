"""sendNotificationIfEnabled called from the front: who may notify whom.

Port of base44/functions/sendNotificationIfEnabled (the internal-key path is gone:
our own services call `notifications.notify` directly). Callers allowed:
  - an admin, or the user himself;
  - a party of `order_id` notifying the OTHER party with a type that side may send
    (a courier cannot write "your order is cancelled" as the customer, nobody can
    notify a stranger through his own order);
  - a courier with a pending offer on `order_id` sending `new_offer` to its customer.
Types are whitelisted, titles capped at 200 chars, bodies at 1000, metadata at 2 KB of
JSON. Answers {success, notification_id, push_skipped?}.

`userId` is an e-mail. A CourierProfile id (the 119 invisible notifications of the
2026-09-28 audit) is mapped to its owner — the stored recipient is always a user.
"""

import json
import re
import uuid
from typing import Any

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Courier, OrderOffer, User
from app.security.deps import CurrentUser
from app.services.messages import courier_user_id, load_order
from app.services.notifications import BODY_MAX, LEGACY_PREFERENCE_KEY, TITLE_MAX, notify_detailed

Result = tuple[int, dict[str, Any]]

TO_CUSTOMER_TYPES = {
    "new_offer", "at_shop", "purchased", "on_the_way", "courier_on_way", "delivered", "order_delivered",
    "eta_update", "delivery_delayed", "new_message", "message",
}  # fmt: skip
TO_COURIER_TYPES = {"order_accepted", "order_cancelled", "new_message", "message"}
METADATA_MAX = 2000  # characters of JSON
_LEGACY_ID = re.compile(r"^[A-Za-z0-9]{8,40}$")


def _cap(value: Any, limit: int) -> str:
    return "" if value is None else str(value)[:limit]


async def _email_of(session: AsyncSession, user_id: uuid.UUID | None) -> str | None:
    if user_id is None:
        return None
    return (await session.execute(select(User.email).where(User.id == user_id))).scalar_one_or_none()


async def recipient_email(session: AsyncSession, ref: str) -> str | None:
    """An e-mail as is; a courier profile id (uuid, or its Base44 id) → its owner's e-mail."""
    if "@" in ref:
        return ref
    as_uuid = None
    try:
        as_uuid = uuid.UUID(ref)
    except ValueError:
        if not _LEGACY_ID.match(ref):
            return None
    column = Courier.id == as_uuid if as_uuid else Courier.legacy_b44_id == ref
    owner = (await session.execute(select(Courier.user_id).where(column))).scalar_one_or_none()
    return await _email_of(session, owner)


async def send_notification_if_enabled(
    session: AsyncSession, caller: CurrentUser, payload: dict[str, Any]
) -> Result:
    type_ = payload.get("type")
    user_ref = payload.get("userId")
    order_raw = payload.get("order_id")
    if not isinstance(user_ref, str) or not user_ref or not isinstance(type_, str) or not type_:
        return 400, {"error": "Missing required fields"}
    if type_ not in LEGACY_PREFERENCE_KEY:
        return 400, {"error": "invalid_type"}
    target_email = await recipient_email(session, user_ref.strip())
    if not target_email:
        return 400, {"error": "invalid_user_id"}
    if order_raw is not None and not isinstance(order_raw, str):
        return 400, {"error": "invalid_order_id"}
    raw_metadata = payload.get("metadata")
    metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
    if len(json.dumps(metadata, ensure_ascii=False, separators=(",", ":"), default=str)) > METADATA_MAX:
        return 400, {"error": "metadata_too_large"}

    target_key = target_email.strip().lower()
    authorized = caller.is_admin or caller.email.lower() == target_key
    order = await load_order(session, order_raw) if order_raw else None

    if not authorized and order_raw:
        if order is None:
            return 404, {"error": "Order not found"}
        customer_email = (await _email_of(session, order.customer_id) or "").lower()
        courier_email = (await _email_of(session, await courier_user_id(session, order)) or "").lower()
        me = caller.email.lower()
        if me in (customer_email, courier_email):
            # Only towards the OTHER party, with a type that side may send.
            if me == customer_email and courier_email and target_key == courier_email and target_key != me:
                authorized = type_ in TO_COURIER_TYPES
            elif me != customer_email and target_key == customer_email and target_key != me:
                authorized = type_ in TO_CUSTOMER_TYPES
        # New-offer flow happens before assignment: a courier with a pending offer on it.
        if not authorized and type_ == "new_offer" and target_key == customer_email:
            authorized = bool(
                (
                    await session.execute(
                        select(
                            exists().where(
                                OrderOffer.order_id == order.id,
                                OrderOffer.status == "pending",
                                OrderOffer.courier_id.in_(
                                    select(Courier.id).where(Courier.user_id == caller.id)
                                ),
                            )
                        )
                    )
                ).scalar()
            )
    if not authorized:
        return 403, {"error": "Forbidden"}
    if order_raw and order is None:
        return 404, {"error": "Order not found"}
    target = (
        await session.execute(
            select(User).where(User.email == target_email.strip(), User.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if target is None:
        return 400, {"error": "invalid_user_id"}

    result = await notify_detailed(
        session,
        user_id=target.id,
        type_=type_,
        title_ar=_cap(payload.get("title_ar"), TITLE_MAX),
        title_fr=_cap(payload.get("title_fr"), TITLE_MAX),
        body_ar=_cap(payload.get("body_ar"), BODY_MAX),
        body_fr=_cap(payload.get("body_fr"), BODY_MAX),
        order_id=order.id if order else None,
        metadata=metadata,
    )
    body: dict[str, Any] = {"success": True, "notification_id": str(result.notification.id)}
    if result.push_skipped:
        body["push_skipped"] = result.push_skipped
    return 200, body
