"""Signed webhooks to the partners (restaurants ODS), queued in `partner_webhook_deliveries`.

One hook in `order_transitions.transition()` (every status change of a partner order) and one in
`offers.create_offer / update_offer / withdraw_offer` queue a row; the job
`deliver_partner_webhooks` (30 s) posts the due rows. Retries after a failure: 1, 2, 4, 8, 16 min
(MAX_ATTEMPTS = 6), then the row is `failed`. The same delivery id goes out at every attempt, so
the receiver deduplicates on `X-ODSD-Delivery-Id`.

Request: POST webhook_url, JSON `{id, event, partner_ref, order_id, created_at, data}` with
`data.order` = the partner's view of the order at send time (app/services/partner_orders.py);
headers `X-ODSD-Event`, `X-ODSD-Delivery-Id`, `X-ODSD-Timestamp` (unix seconds) and
`X-ODSD-Signature: sha256=<hex HMAC-SHA256(secret, "<timestamp>.<raw body>")>` (secret decrypted from
`partners.webhook_secret`, app/security/secret_box.py) — the timestamp is
inside the signature (replay window ± 5 min on the receiver's side). HTTPS only outside local.
The HTTP transport can be replaced (tests): `set_transport(httpx.MockTransport(...))`.
"""

import hashlib
import hmac
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import NoResponseCase, Order, OrderOffer, Partner, PartnerWebhookDelivery
from app.security import secret_box

log = logging.getLogger("odsd.partner_webhooks")

BACKOFF_MINUTES = (1, 2, 4, 8, 16)
MAX_ATTEMPTS = len(BACKOFF_MINUTES) + 1
BATCH = 50
RETURN_REASONS = frozenset({"returned_to_shop", "goods_returned_to_shop"})
EXPIRY_REASONS = frozenset({"expired", "expired_no_offer", "abandoned"})
SIGNATURE_HEADER = "X-ODSD-Signature"
TIMESTAMP_HEADER = "X-ODSD-Timestamp"
EVENT_HEADER = "X-ODSD-Event"
DELIVERY_HEADER = "X-ODSD-Delivery-Id"

_transport: httpx.AsyncBaseTransport | None = None


def set_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    global _transport
    _transport = transport


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def sign(secret: str, timestamp: int | str, body: bytes) -> str:
    digest = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify(
    secret: str, timestamp: str, body: bytes, header: str, now: int | None = None, tolerance: int = 300
) -> bool:
    """What a receiver does (used by the tests and offered to the ODS side as a reference)."""
    try:
        sent = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs((now or int(datetime.now(UTC).timestamp())) - sent) > tolerance:
        return False
    return hmac.compare_digest(sign(secret, sent, body), header or "")


def _json(value: Any) -> Any:
    if isinstance(value, datetime):
        return iso(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if hasattr(value, "__float__") and not isinstance(value, bool | int | float):
        return float(value)
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


def dumps(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, default=_json, ensure_ascii=False, separators=(",", ":")).encode()


async def queue(
    session: AsyncSession,
    partner: Partner,
    event: str,
    order: Order | None = None,
    data: dict[str, Any] | None = None,
) -> PartnerWebhookDelivery | None:
    """Adds the event to the partner's queue (None when the partner has no webhook URL)."""
    if not partner.webhook_url:
        return None
    delivery_id = uuid.uuid4()
    envelope = {
        "id": str(delivery_id),
        "event": event,
        "partner_ref": order.partner_ref if order is not None else None,
        "order_id": str(order.id) if order is not None else None,
        "created_at": iso(datetime.now(UTC)),
        "data": json.loads(dumps(data or {})),
    }
    row = PartnerWebhookDelivery(
        id=delivery_id,
        partner_id=partner.id,
        order_id=order.id if order is not None else None,
        event=event,
        payload=envelope,
        status="pending",
        attempts=0,
        next_attempt_at=datetime.now(UTC),
    )
    session.add(row)
    await session.flush()
    return row


# --- hooks ----------------------------------------------------------------------------------------


def transition_events(
    from_status: str | None, to_status: str, reason: str | None, cancelled_by: str | None
) -> list[tuple[str, dict[str, Any]]]:
    """The events a status change of a partner order produces (pure)."""
    if to_status == "pending":
        if cancelled_by:  # the courier dropped the delivery: back to the pool
            return [
                ("order.cancelled", {"cancelled_by": cancelled_by, "reason": reason, "back_to_pool": True})
            ]
        return []  # last offer withdrawn (order.offer_withdrawn already said it)
    if to_status in ("offers_received", "price_confirmation_needed"):
        return []
    if to_status == "on_the_way":
        return [("order.on_the_way", {"resumed": from_status == "client_no_response"})]
    if to_status in ("accepted", "at_shop", "purchased", "delivered"):
        return [(f"order.{to_status}", {})]
    if to_status == "client_no_response":
        return [("order.no_response", {})]
    if to_status == "cancelled":
        events: list[tuple[str, dict[str, Any]]] = []
        if cancelled_by == "system" and reason in EXPIRY_REASONS:
            events.append(("order.expired", {"reason": reason}))
        else:
            events.append(
                ("order.cancelled", {"cancelled_by": cancelled_by, "reason": reason, "back_to_pool": False})
            )
        if reason in RETURN_REASONS:
            events.append(("order.returned_to_shop", {}))
        return events
    return []


async def on_transition(
    session: AsyncSession,
    order: Order,
    from_status: str | None,
    to_status: str,
    reason: str | None,
    cancelled_by: str | None,
) -> None:
    """Called by order_transitions.transition() for a partner order (order locked, flushed)."""
    from app.services import partner_stats

    partner = await session.get(Partner, order.partner_id)
    if partner is None:
        return
    events = transition_events(from_status, to_status, reason, cancelled_by)
    for event, data in events:
        if event == "order.no_response":
            case = (
                await session.execute(
                    select(NoResponseCase)
                    .where(NoResponseCase.order_id == order.id)
                    .order_by(NoResponseCase.created_at.desc(), NoResponseCase.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if case is not None:
                from app.services import no_response

                data = {
                    "deadline_at": iso(case.deadline_at),
                    "wait_seconds": int((case.deadline_at - case.started_at).total_seconds()),
                    # D2: when the meal must be back at the restaurant (30 / 45 min, ≤ 60 after payment)
                    "return_deadline_at": iso(
                        await no_response.return_deadline(session, order, case, datetime.now(UTC))
                    ),
                }
        elif event == "order.accepted":
            from app.services import partner_orders

            # the contract's `courier` object (first name, rating, deliveries, vehicle, phone for the
            # restaurant), never the internal courier id
            data = {
                "delivery_fee": order.delivery_fee,
                "courier": await partner_orders.courier_card(session, order.courier_id, contact=True),
            }
        elif event == "order.returned_to_shop":
            data = {"purchase_amount": order.purchase_amount, "delivery_fee": order.delivery_fee}
            if order.partner_return_outcome is None:
                order.partner_return_outcome = "accepted"
        await queue(session, partner, event, order, data)
    if to_status in ("delivered", "cancelled"):
        await partner_stats.refresh(session, order.contact_phone_e164)


async def on_offer(
    session: AsyncSession, order: Order, offer: OrderOffer, kind: str, previous_fee: Any = None
) -> None:
    """offers.create_offer / update_offer / withdraw_offer → order.offer_received / _updated / _withdrawn."""
    if order.partner_id is None:
        return
    from app.services import partner_orders

    partner = await session.get(Partner, order.partner_id)
    if partner is None:
        return
    if kind == "withdrawn":
        data: dict[str, Any] = {"offer": {"offer_id": str(offer.id)}}
    else:
        data = {"offer": await partner_orders.offer_view(session, offer)}
        if previous_fee is not None:
            data["previous_fee"] = previous_fee
    await queue(session, partner, f"order.offer_{kind}", order, data)


# --- delivery -------------------------------------------------------------------------------------


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=settings.PARTNER_WEBHOOK_TIMEOUT_SECONDS, transport=_transport)


def _url_allowed(url: str) -> bool:
    return url.startswith("https://") or (settings.is_local and url.startswith("http://"))


async def deliver(session: AsyncSession, delivery: PartnerWebhookDelivery, client: httpx.AsyncClient) -> bool:
    """One attempt. Updates the row (delivered / rescheduled / failed); True when accepted (2xx)."""
    from app.services import partner_orders

    partner = await session.get(Partner, delivery.partner_id)
    now = datetime.now(UTC)
    delivery.attempts += 1
    if partner is None or not partner.webhook_url or not partner.webhook_secret:
        delivery.status, delivery.last_error = "failed", "webhook_not_configured"
        await session.flush()
        return False
    payload = dict(delivery.payload)
    data = dict(payload.get("data") or {})
    if delivery.order_id is not None:
        order = await session.get(Order, delivery.order_id)
        if delivery.event.startswith("vitrine."):
            from app.services import vitrine_orders  # no customer data in a Vitrine event

            data["order"] = await vitrine_orders.order_view(session, order) if order is not None else None
        else:
            data["order"] = await partner_orders.order_view(session, order) if order is not None else None
    payload["data"] = data
    body = dumps(payload)
    timestamp = int(now.timestamp())
    status: int | None = None
    error: str | None = None
    try:
        secret = secret_box.decrypt(partner.webhook_secret) or ""
    except secret_box.SecretKeyError as exc:
        log.error("partner %s: webhook secret unreadable: %s", partner.id, exc)
        secret, error = "", "secret_unreadable"
    headers = {
        "Content-Type": "application/json",
        EVENT_HEADER: delivery.event,
        DELIVERY_HEADER: str(delivery.id),
        TIMESTAMP_HEADER: str(timestamp),
        SIGNATURE_HEADER: sign(secret, timestamp, body),
        "User-Agent": "ODS-Delivery-Webhooks/1.0",
    }
    if error is None and not _url_allowed(partner.webhook_url):
        error = "https_required"
    if error is None:
        try:
            response = await client.post(partner.webhook_url, content=body, headers=headers)
            status = response.status_code
            if not 200 <= status < 300:
                error = f"http_{status}"
        except httpx.HTTPError as exc:
            error = type(exc).__name__
    delivery.last_status, delivery.last_error = status, error
    if error is None:
        delivery.status, delivery.delivered_at = "delivered", now
    elif delivery.attempts >= MAX_ATTEMPTS:
        delivery.status = "failed"
    else:
        delivery.next_attempt_at = now + timedelta(minutes=BACKOFF_MINUTES[delivery.attempts - 1])
    await session.flush()
    return error is None


async def deliver_due(session: AsyncSession, limit: int = BATCH) -> dict[str, int]:
    """The job's step: due pending rows, oldest first, one HTTP call each."""
    rows = list(
        (
            await session.execute(
                select(PartnerWebhookDelivery)
                .where(
                    PartnerWebhookDelivery.status == "pending",
                    PartnerWebhookDelivery.next_attempt_at <= datetime.now(UTC),
                )
                .order_by(PartnerWebhookDelivery.next_attempt_at, PartnerWebhookDelivery.created_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).scalars()
    )
    delivered = failed = 0
    if not rows:
        return {"due": 0, "delivered": 0, "failed": 0}
    async with _client() as client:
        for row in rows:
            if await deliver(session, row, client):
                delivered += 1
            else:
                failed += 1
    return {"due": len(rows), "delivered": delivered, "failed": failed}


async def deliver_now(session: AsyncSession, delivery: PartnerWebhookDelivery) -> dict[str, Any]:
    """testPartnerWebhook / POST /webhook-test: one attempt right away, the outcome answered."""
    async with _client() as client:
        ok = await deliver(session, delivery, client)
    return {
        "delivered": ok,
        "status_code": delivery.last_status,
        "error": delivery.last_error,
        "delivery_id": str(delivery.id),
    }
