"""Partners administration (Admin › Partenaires) and the D4/D5 return report — Lot 2, 2A.

Admin only (same guard as app/services/launch_mode.py): createPartner, listPartners,
updatePartner, revokePartner, rotatePartnerKey, rotatePartnerWebhookSecret, testPartnerWebhook,
settlePartnerDebt, listPartnerWebhookDeliveries. The key and the webhook secret are answered ONCE,
at creation / rotation; the key is kept as sha256 only, the secret encrypted
(app/security/secret_box.py: the server needs it in clear to sign).
reportPartnerReturn (the order's courier or an admin): the restaurant refused the returned meal
(D5 counter) or was closed (D4 debt = advance + delivery fee; no new order until settled).
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.errors import ApiError
from app.models import Order, Partner, PartnerWebhookDelivery, User
from app.models.partners import MAX_OPEN_ORDERS_LIMIT
from app.realtime.events import emit
from app.security import secret_box
from app.security.deps import CurrentUser
from app.security.partner import generate_key, generate_secret, hash_key, key_prefix
from app.services import order_transitions as ot
from app.services import partner_webhooks
from app.services.orders import courier_of_user

NAME_MAX = 120
WEBHOOK_URL_MAX = 500
DELIVERIES_MAX = 100
TECHNICAL_EMAIL_DOMAIN = "partner.ods-delivery.local"
RETURN_OUTCOMES_REPORTED = ("refused", "shop_closed")


def iso(value: datetime | None) -> str | None:
    return partner_webhooks.iso(value)


def _admin(user: CurrentUser) -> None:
    if not user.is_admin:
        raise ApiError(403, "forbidden", "Admin only")


def _name(value: Any) -> str:
    name = str(value or "").strip()
    if not 1 <= len(name) <= NAME_MAX:
        raise ApiError(400, "validation_error", f"name: 1 to {NAME_MAX} characters")
    return name


def _webhook_url(value: Any) -> str | None:
    if value is None or value == "":
        return None
    url = str(value).strip()
    parts = urlsplit(url)
    if len(url) > WEBHOOK_URL_MAX or parts.scheme not in ("http", "https") or not parts.netloc:
        raise ApiError(400, "validation_error", "webhook_url: a valid http(s) URL")
    if parts.scheme != "https" and not settings.is_local:
        raise ApiError(400, "webhook_url_https_required", "webhook_url must use https")
    return url


def _max_open(value: Any, default: int = 50) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_OPEN_ORDERS_LIMIT:
        raise ApiError(400, "validation_error", f"max_open_orders: 1 to {MAX_OPEN_ORDERS_LIMIT}")
    return value


def _restaurant_id(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ApiError(400, "validation_error", "ods_restaurant_id: a positive integer")
    return value


async def _partner(session: AsyncSession, raw_id: Any, *, lock: bool = False) -> Partner:
    try:
        pid = uuid.UUID(str(raw_id))
    except (TypeError, ValueError):
        raise ApiError(404, "partner_not_found", "Partner not found") from None
    stmt = select(Partner).where(Partner.id == pid)
    if lock:
        stmt = stmt.with_for_update()
    partner = (await session.execute(stmt)).scalar_one_or_none()
    if partner is None:
        raise ApiError(404, "partner_not_found", "Partner not found")
    return partner


async def partner_json(session: AsyncSession, partner: Partner) -> dict[str, Any]:
    open_count = int(
        (
            await session.execute(
                select(func.count())
                .select_from(Order)
                .where(Order.partner_id == partner.id, Order.status.in_(ot.OPEN_STATUSES))
            )
        ).scalar_one()
    )
    return {
        "id": str(partner.id),
        "name": partner.name,
        "kind": partner.kind,
        "ods_restaurant_id": partner.ods_restaurant_id,
        "key_prefix": partner.key_prefix,
        "webhook_url": partner.webhook_url,
        "has_webhook_secret": bool(partner.webhook_secret),
        "max_open_orders": partner.max_open_orders,
        "active": partner.active,
        "revoked_at": iso(partner.revoked_at),
        "created_at": iso(partner.created_at),
        "last_used_at": iso(partner.last_used_at),
        "technical_user_id": str(partner.technical_user_id),
        "open_orders": open_count,
        "returns_refused": partner.returns_refused,
        "debt_amount": float(partner.debt_amount or 0),
        "debt_since": iso(partner.debt_since),
    }


# --- admin functions ------------------------------------------------------------------------------


async def create_partner(
    session: AsyncSession, admin: CurrentUser, payload: dict[str, Any]
) -> dict[str, Any]:
    _admin(admin)
    name = _name(payload.get("name"))
    webhook_url = _webhook_url(payload.get("webhook_url"))
    max_open = _max_open(payload.get("max_open_orders"))
    restaurant_id = _restaurant_id(payload.get("ods_restaurant_id"))
    now = datetime.now(UTC)
    partner_id = uuid.uuid4()
    technical = User(
        email=f"partner-{partner_id.hex[:12]}@{TECHNICAL_EMAIL_DOMAIN}",
        email_verified_at=now,
        password_hash=None,
        full_name=name,
        role="customer",
        language="fr",
        profile_created_at=now,
        push_enabled=False,
        notify_order_status=False,
        notify_new_orders=False,
        notify_incoming_orders=False,
        notify_chat=False,
        terms_accepted_at=now,
        terms_version="partner",
    )
    session.add(technical)
    await session.flush()
    key = generate_key()
    secret = generate_secret()
    partner = Partner(
        id=partner_id,
        name=name,
        kind="ods_restaurant",
        ods_restaurant_id=restaurant_id,
        key_prefix=key_prefix(key),
        key_hash=hash_key(key),
        technical_user_id=technical.id,
        webhook_url=webhook_url,
        webhook_secret=secret_box.encrypt(secret),  # signing secret, encrypted at rest
        max_open_orders=max_open,
        active=True,
        created_by=admin.id,
    )
    session.add(partner)
    await session.flush()
    await session.refresh(partner)
    return {
        "success": True,
        "partner": await partner_json(session, partner),
        "api_key": key,
        "webhook_secret": secret,
    }


async def list_partners(session: AsyncSession, admin: CurrentUser, payload: dict[str, Any]) -> dict[str, Any]:
    _admin(admin)
    stmt = select(Partner).order_by(Partner.created_at.desc())
    if not payload.get("include_revoked"):
        stmt = stmt.where(Partner.active.is_(True))
    partners = (await session.execute(stmt)).scalars()
    return {"success": True, "partners": [await partner_json(session, p) for p in partners]}


async def update_partner(
    session: AsyncSession, admin: CurrentUser, payload: dict[str, Any]
) -> dict[str, Any]:
    _admin(admin)
    partner = await _partner(session, payload.get("partner_id"), lock=True)
    if "name" in payload:
        partner.name = _name(payload["name"])
    if "webhook_url" in payload:
        partner.webhook_url = _webhook_url(payload["webhook_url"])
    if "max_open_orders" in payload:
        partner.max_open_orders = _max_open(payload["max_open_orders"], partner.max_open_orders)
    if "ods_restaurant_id" in payload:
        partner.ods_restaurant_id = _restaurant_id(payload["ods_restaurant_id"])
    await session.flush()
    return {"success": True, "partner": await partner_json(session, partner)}


async def revoke_partner(
    session: AsyncSession, admin: CurrentUser, payload: dict[str, Any]
) -> dict[str, Any]:
    _admin(admin)
    partner = await _partner(session, payload.get("partner_id"), lock=True)
    if partner.active:
        partner.active, partner.revoked_at = False, datetime.now(UTC)
        await session.flush()
    return {"success": True, "partner": await partner_json(session, partner)}


async def rotate_key(session: AsyncSession, admin: CurrentUser, payload: dict[str, Any]) -> dict[str, Any]:
    _admin(admin)
    partner = await _partner(session, payload.get("partner_id"), lock=True)
    if not partner.active:
        raise ApiError(409, "partner_revoked", "A revoked partner gets no new key")
    key = generate_key()
    partner.key_prefix, partner.key_hash = key_prefix(key), hash_key(key)
    await session.flush()
    return {"success": True, "partner": await partner_json(session, partner), "api_key": key}


async def rotate_webhook_secret(
    session: AsyncSession, admin: CurrentUser, payload: dict[str, Any]
) -> dict[str, Any]:
    _admin(admin)
    partner = await _partner(session, payload.get("partner_id"), lock=True)
    secret = generate_secret()
    partner.webhook_secret = secret_box.encrypt(secret)
    await session.flush()
    return {"success": True, "partner": await partner_json(session, partner), "webhook_secret": secret}


async def test_webhook(session: AsyncSession, admin: CurrentUser, payload: dict[str, Any]) -> dict[str, Any]:
    _admin(admin)
    partner = await _partner(session, payload.get("partner_id"))
    return {"success": True, **await send_ping(session, partner)}


async def send_ping(session: AsyncSession, partner: Partner) -> dict[str, Any]:
    """A `ping` event delivered right away (testPartnerWebhook, POST /webhook-test)."""
    if not partner.webhook_url:
        raise ApiError(409, "webhook_not_configured", "No webhook URL on this partner")
    delivery = await partner_webhooks.queue(
        session, partner, "ping", None, {"partner": {"id": str(partner.id), "name": partner.name}}
    )
    assert delivery is not None
    return await partner_webhooks.deliver_now(session, delivery)


async def settle_debt(session: AsyncSession, admin: CurrentUser, payload: dict[str, Any]) -> dict[str, Any]:
    _admin(admin)
    partner = await _partner(session, payload.get("partner_id"), lock=True)
    if partner.debt_amount and partner.debt_amount > 0:
        settled = partner.debt_amount
        partner.debt_amount, partner.debt_since = Decimal("0"), None
        await session.flush()
        await partner_webhooks.queue(session, partner, "partner.debt_settled", None, {"amount": settled})
    return {"success": True, "partner": await partner_json(session, partner)}


async def list_deliveries(
    session: AsyncSession, admin: CurrentUser, payload: dict[str, Any]
) -> dict[str, Any]:
    _admin(admin)
    partner = await _partner(session, payload.get("partner_id"))
    limit = payload.get("limit")
    limit = (
        limit
        if isinstance(limit, int) and not isinstance(limit, bool) and 1 <= limit <= DELIVERIES_MAX
        else 20
    )
    rows = (
        await session.execute(
            select(PartnerWebhookDelivery)
            .where(PartnerWebhookDelivery.partner_id == partner.id)
            .order_by(PartnerWebhookDelivery.created_at.desc())
            .limit(limit)
        )
    ).scalars()
    return {
        "success": True,
        "deliveries": [
            {
                "id": str(d.id),
                "event": d.event,
                "order_id": str(d.order_id) if d.order_id else None,
                "status": d.status,
                "attempts": d.attempts,
                "next_attempt_at": iso(d.next_attempt_at) if d.status == "pending" else None,
                "delivered_at": iso(d.delivered_at),
                "last_status": d.last_status,
                "last_error": d.last_error,
                "created_at": iso(d.created_at),
            }
            for d in rows
        ],
    }


# --- D4 / D5: the courier brings the meal back ----------------------------------------------------


async def report_return(session: AsyncSession, user: CurrentUser, payload: dict[str, Any]) -> dict[str, Any]:
    """reportPartnerReturn {order_id, outcome: refused | shop_closed} by the order's courier or an admin."""
    outcome = payload.get("outcome")
    if outcome not in RETURN_OUTCOMES_REPORTED:
        raise ApiError(400, "validation_error", "outcome: refused or shop_closed")
    order = await ot.lock_order(session, payload.get("order_id"))
    if order is None:
        raise ApiError(404, "order_not_found", "Order not found")
    if not user.is_admin:
        mine = await courier_of_user(session, user.id)
        if mine is None or order.courier_id != mine.id:
            raise ApiError(403, "forbidden", "Only the order's courier")
    if order.partner_id is None or order.source != "partner":
        raise ApiError(409, "not_partner_order", "Not a restaurant order")
    after_no_response = order.status == "client_no_response" or (
        order.status == "cancelled" and order.cancelled_by == "courier"
    )
    if not after_no_response:
        error = ApiError(409, "not_returnable", "Only after « client ne répond pas »")
        error.extra = {"status": order.status}
        raise error
    if order.partner_return_outcome is not None:
        raise ApiError(409, "already_reported", "Already reported", outcome=order.partner_return_outcome)
    partner = await _partner(session, order.partner_id, lock=True)
    order.partner_return_outcome = outcome
    if outcome == "refused":
        partner.returns_refused += 1
        await session.flush()
        await partner_webhooks.queue(
            session, partner, "order.return_refused", order, {"returns_refused": partner.returns_refused}
        )
    else:
        amount = (order.purchase_amount or Decimal("0")) + (order.delivery_fee or Decimal("0"))
        partner.debt_amount = (partner.debt_amount or Decimal("0")) + amount
        partner.debt_since = partner.debt_since or datetime.now(UTC)
        await session.flush()
        await partner_webhooks.queue(
            session,
            partner,
            "order.restaurant_debt",
            order,
            {"amount": amount, "debt_amount": partner.debt_amount},
        )
    emit(session, "Order", "update", order.id)
    return {
        "success": True,
        "outcome": outcome,
        "returns_refused": partner.returns_refused,
        "debt_amount": float(partner.debt_amount or 0),
    }
