"""Push notifications to a user's devices.

Providers:
- `fcm`: firebase-admin, one `send_each_for_multicast` per locale (in a thread, with a timeout);
- `log`: sends nothing (tests, machines without credentials).
Both write one `push_deliveries` row per device. Payload shape, Android channel and web
link are those of base44/functions/sendPushToTokens. Token upkeep is the same too: dead
tokens (UNREGISTERED / INVALID_ARGUMENT / NOT_FOUND) are deactivated at once, other
failures after 5 in a row. Preference checks (push_enabled...) belong to the caller.
"""

import asyncio
import json
import logging
import os
import uuid
import warnings
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import firebase_admin
from firebase_admin import credentials, messaging
from firebase_admin import exceptions as fb_exceptions
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import DeviceToken, PushDelivery
from app.security.tokens import now_utc

log = logging.getLogger("odsd.push")
FAILURE_THRESHOLD = 5
MAX_TOKENS_PER_USER = 20
FCM_BATCH = 500

Status = Literal["sent", "failed", "invalid_token"]

COURIER_TYPES = {"new_order", "customer_responded", "customer_no_response_final", "hot_deal_reserved"}
CUSTOMER_TYPES = {
    "new_offer", "order_confirmed", "order_preparing", "at_shop", "purchased", "on_the_way",
    "courier_on_way", "eta_update", "delivered", "order_delivered", "delivery_delayed",
    "delivery_cancelled", "emergency_contact", "issue_reported",
}  # fmt: skip


@dataclass(frozen=True)
class PushMessage:
    type: str
    title_ar: str
    title_fr: str
    body_ar: str
    body_fr: str
    order_id: str | None = None
    notification_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Outcome:
    token_id: uuid.UUID
    status: Status
    error: str | None = None


def recipient_role(type_: str, metadata: dict[str, Any]) -> str | None:
    """Which side of the app a push is for (mirrors src/lib/notifications/notificationRole.js)."""
    role = metadata.get("recipient_role")
    if role in ("customer", "courier"):
        return role
    if type_ in COURIER_TYPES:
        return "courier"
    if type_ in CUSTOMER_TYPES:
        return "customer"
    if type_ in ("new_message", "message"):
        return {"courier": "customer", "customer": "courier"}.get(metadata.get("sender_role", ""))
    if type_ == "order_accepted":
        if metadata.get("status") or metadata.get("notification_type"):
            return "customer"
        if metadata.get("offer_id") or metadata.get("shop_name") or metadata.get("delivery_address"):
            return "courier"
        return None
    if type_ == "order_cancelled":
        has_ctx = metadata.get("status") or metadata.get("notification_type") or metadata.get("ctx")
        return "customer" if has_ctx else "courier"
    return None


def push_link(type_: str, order_id: str | None, metadata: dict[str, Any]) -> str:
    """In-app link; `as=<role>` makes a dual account switch to that side first."""
    role = recipient_role(type_, metadata)
    if role == "courier":
        if not order_id:
            path = "/CourierHome"
        elif type_ == "new_order":
            path = f"/CourierOrderDetail?id={order_id}"
        else:
            path = f"/CourierOrderActive?id={order_id}"
    elif not order_id:
        path = "/"
    elif type_ == "new_offer":
        path = f"/OrderOffers?id={order_id}"
    else:
        path = f"/OrderTracking?id={order_id}"
    if not role:
        return path
    return f"{path}{'&' if '?' in path else '?'}as={role}"


def build_multicast(tokens: list[str], locale: str, msg: PushMessage) -> messaging.MulticastMessage:
    is_ar = locale != "fr"
    collapse = f"order_{msg.order_id}" if msg.order_id else f"notif_{msg.notification_id}"
    link = push_link(msg.type, msg.order_id, msg.metadata)
    with warnings.catch_warnings():
        # firebase-admin 7 deprecates `tokens` in favour of `fids` (installation ids), a different
        # identifier: the app stores FCM registration tokens, which FCM still accepts here.
        warnings.simplefilter("ignore", DeprecationWarning)
        return _multicast(tokens, msg, is_ar, collapse, link)


def _multicast(tokens: list[str], msg: PushMessage, is_ar: bool, collapse: str, link: str) -> Any:
    return messaging.MulticastMessage(
        tokens=tokens,
        notification=messaging.Notification(
            title=msg.title_ar if is_ar else msg.title_fr, body=msg.body_ar if is_ar else msg.body_fr
        ),
        data={
            "type": msg.type,
            "notification_id": msg.notification_id or "",
            "order_id": msg.order_id or "",
            "title_ar": msg.title_ar,
            "title_fr": msg.title_fr,
            "body_ar": msg.body_ar,
            "body_fr": msg.body_fr,
            "metadata_json": json.dumps(msg.metadata, ensure_ascii=False),
            "click_action": link,
        },
        android=messaging.AndroidConfig(
            priority="high",
            collapse_key=collapse,
            notification=messaging.AndroidNotification(
                channel_id="new_orders" if msg.type == "new_order" else "default",
                sound="default",
                tag=collapse,
            ),
        ),
        apns=messaging.APNSConfig(
            headers={"apns-priority": "10", "apns-collapse-id": collapse},
            payload=messaging.APNSPayload(
                aps=messaging.Aps(sound="default", mutable_content=True, thread_id=collapse)
            ),
        ),
        webpush=messaging.WebpushConfig(
            headers={"Urgency": "high", "TTL": "3600"},
            notification=messaging.WebpushNotification(
                icon="/icons/icon-192.png",
                badge="/icons/icon-192.png",
                tag=collapse,
                require_interaction=msg.type == "new_order",
            ),
            fcm_options=messaging.WebpushFCMOptions(link=link),
        ),
    )


class PushProvider(Protocol):
    name: Literal["fcm", "log"]

    async def send(self, tokens: Sequence[DeviceToken], msg: PushMessage) -> list[Outcome]: ...


class LogProvider:
    name: Literal["fcm", "log"] = "log"

    async def send(self, tokens: Sequence[DeviceToken], msg: PushMessage) -> list[Outcome]:
        log.info("push (log provider) type=%s devices=%d", msg.type, len(tokens))
        return [Outcome(t.id, "sent") for t in tokens]


def _is_dead_token(exc: BaseException | None) -> bool:
    if isinstance(
        exc, (messaging.UnregisteredError, fb_exceptions.InvalidArgumentError, fb_exceptions.NotFoundError)
    ):
        return True
    code = getattr(exc, "code", "") or ""
    return str(code).upper() in {"UNREGISTERED", "INVALID_ARGUMENT", "NOT_FOUND"}


class FcmProvider:
    name: Literal["fcm", "log"] = "fcm"

    async def send(self, tokens: Sequence[DeviceToken], msg: PushMessage) -> list[Outcome]:
        outcomes: list[Outcome] = []
        by_locale: dict[str, list[DeviceToken]] = {}
        for token in tokens:
            by_locale.setdefault("fr" if token.locale == "fr" else "ar", []).append(token)
        for locale, group in by_locale.items():
            for start in range(0, len(group), FCM_BATCH):
                batch = group[start : start + FCM_BATCH]
                outcomes += await self._send_batch(batch, locale, msg)
        return outcomes

    async def _send_batch(self, batch: list[DeviceToken], locale: str, msg: PushMessage) -> list[Outcome]:
        multicast = build_multicast([t.token for t in batch], locale, msg)
        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(messaging.send_each_for_multicast, multicast),
                timeout=settings.PUSH_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            reason = "timeout" if isinstance(exc, TimeoutError) else type(exc).__name__
            return [Outcome(t.id, "failed", reason) for t in batch]
        outcomes = []
        for token, item in zip(batch, response.responses, strict=True):
            if item.success:
                outcomes.append(Outcome(token.id, "sent"))
            else:
                status: Status = "invalid_token" if _is_dead_token(item.exception) else "failed"
                outcomes.append(Outcome(token.id, status, str(item.exception)[:300]))
        return outcomes


def init_firebase() -> bool:
    """Initializes firebase-admin from FIREBASE_CREDENTIALS_PATH; False (push via `log`) without it."""
    if firebase_admin._apps:
        return True
    path = settings.FIREBASE_CREDENTIALS_PATH
    if not path or not os.path.isfile(path):
        log.info("no Firebase credentials: push uses the log provider")
        return False
    try:
        cred = credentials.Certificate(path)
        firebase_admin.initialize_app(cred)
    except Exception:
        log.exception("Firebase init failed: push uses the log provider")
        return False
    log.info("Firebase initialised (project %s)", cred.project_id)
    return True


def get_provider() -> PushProvider:
    if settings.PUSH_PROVIDER == "log":
        return LogProvider()
    if settings.PUSH_PROVIDER == "fcm" or firebase_admin._apps:
        return FcmProvider()
    return LogProvider()


async def send_to_user(
    session: AsyncSession, user_id: uuid.UUID, msg: PushMessage, provider: PushProvider | None = None
) -> dict[str, Any]:
    """Pushes `msg` to the user's active devices and records each attempt. The caller commits."""
    tokens = list(
        (
            await session.execute(
                select(DeviceToken)
                .where(DeviceToken.user_id == user_id, DeviceToken.is_active)
                .order_by(DeviceToken.last_seen_at.desc())
                .limit(MAX_TOKENS_PER_USER)
            )
        ).scalars()
    )
    chosen = provider or get_provider()
    if not tokens:
        return {"provider": chosen.name, "attempted": 0, "delivered": 0}
    outcomes = await chosen.send(tokens, msg)
    by_id = {t.id: t for t in tokens}
    now = now_utc()
    notification_id = uuid.UUID(msg.notification_id) if msg.notification_id else None
    for outcome in outcomes:
        token = by_id[outcome.token_id]
        if outcome.status == "sent":
            token.failure_count, token.last_error, token.last_seen_at = 0, None, now
        elif outcome.status == "invalid_token":
            token.is_active, token.last_error = False, (outcome.error or "invalid_token")[:200]
        else:
            token.failure_count += 1
            token.last_error = (outcome.error or "unknown")[:200]
            token.is_active = token.failure_count < FAILURE_THRESHOLD
        session.add(
            PushDelivery(
                user_id=user_id,
                device_token_id=token.id,
                notification_id=notification_id,
                provider=chosen.name,
                status=outcome.status,
                error=outcome.error,
                payload={
                    "type": msg.type,
                    "order_id": msg.order_id,
                    "link": push_link(msg.type, msg.order_id, msg.metadata),
                },
            )
        )
    await session.flush()
    delivered = sum(1 for o in outcomes if o.status == "sent")
    return {"provider": chosen.name, "attempted": len(outcomes), "delivered": delivered}
