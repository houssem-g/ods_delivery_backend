"""sendWhatsAppMessage — WhatsApp template (Meta) with SMS fallback (WinSMS), by hand.

Our services call
app/services/whatsapp.py directly, so over HTTP it is **admin only** (the setup guide's
manual checks: a test send, `check_pending`, `summary`). Everyone else: 403 Forbidden.

Body: { action?: send|fallback|check_pending|summary, ... } — see the service. `user_id`
may be an e-mail or a user id.
"""

import uuid
from typing import Any

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Notification, Order, User
from app.security.deps import CurrentUser
from app.services import whatsapp


def _uuid(raw: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(raw)) if raw else None
    except ValueError:
        return None


async def _existing(session: AsyncSession, model: Any, raw: Any) -> uuid.UUID | None:
    row_id = _uuid(raw)
    return row_id if row_id and await session.get(model, row_id) is not None else None


async def _user_id(session: AsyncSession, raw: Any) -> uuid.UUID | None:
    if not raw:
        return None
    if "@" in str(raw):
        return (await session.execute(select(User.id).where(User.email == str(raw)))).scalar_one_or_none()
    return await _existing(session, User, raw)


async def handle(
    payload: dict[str, Any], user: CurrentUser | None, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    if user is None or not user.is_admin:
        return 403, {"error": "Forbidden"}
    action = payload.get("action") or "send"
    if action == "send":
        return await whatsapp.send_template(
            session,
            template_key=str(payload.get("template_key") or ""),
            params=payload.get("params"),
            idempotency_key=str(payload.get("idempotency_key") or ""),
            to=str(payload["to"]) if payload.get("to") else None,
            user_id=await _user_id(session, payload.get("user_id")),
            order_id=await _existing(session, Order, payload.get("order_id")),
            notification_id=await _existing(session, Notification, payload.get("notification_id")),
            lang=payload.get("lang"),
            critical=bool(payload.get("critical")),
        )
    if action == "fallback":
        return await whatsapp.fallback(session, payload.get("log_id"), payload.get("reason"))
    if action == "check_pending":
        return await whatsapp.check_pending(session, _uuid(payload.get("order_id")))
    if action == "summary":
        return await whatsapp.summary(session, payload.get("idempotency_key"))
    return 400, {"error": "unknown action"}
