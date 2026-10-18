"""A Vitrine order reaches the restaurant's ODS INSIDE the customer's request: all or nothing (owner 18/10:
« si la commande est confirmée, c'est passé et ça doit déjà apparaître chez le restaurateur »).

When the order becomes confirmed — « Je viens chercher » sent (placeOrder), or a courier's offer accepted
for a delivered Vitrine order (acceptOrderOffer) — the customer's transaction:

1. takes the order's lock (advisory, by its reference: a double click waits for the first one);
2. writes the order (flushed, NOT committed: no one else sees it — the « pending row invisible to all »);
3. sends the signed `vitrine.order_confirmed` to ODS NOW (timeout 8 s) and requires ODS's answer « ok, order
   n° … »;
4. ODS said yes → the customer's transaction commits at once and the customer sees « Reçue »; ODS said no /
   did not answer → the transaction rolls back: nothing at Delivery; the customer reads « Le restaurant n'a
   pas pu recevoir votre commande, réessayez », his cart intact.

The reference is deterministic (a pickup order: from the customer and the cart's `client_ref`; a delivered
order: its id), so a retry is idempotent at ODS (its order is found again, never a second one) and at
Delivery (the order found again, returned as is).

Compensation (never a ghost order in the kitchen that the customer does not see): when ODS may have created
the order but Delivery did not keep it (a timeout, a lost answer, Delivery's commit failing after ODS's «
yes »), Delivery sends ODS a signed `vitrine.order_cancelled {reason: "not_confirmed"}` right away (4 s),
under the same lock and only if no retry went through meanwhile; ODS hides that order (never on the board),
and revives it if the customer's retry comes with the same reference. If even that call fails, it is queued
(retried, and dropped as obsolete as soon as a retry went through). This is the only queue left for the
creation: the creation itself is never queued nor retried, and nothing is cancelled by time."""

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Order, Partner, PartnerWebhookDelivery
from app.services import partner_webhooks
from app.services.orders import OrderRefused

log = logging.getLogger("odsd.vitrine_sync")

CONFIRMED = "vitrine.order_confirmed"
CANCELLED = "vitrine.order_cancelled"
NOT_CONFIRMED = "not_confirmed"
TIMEOUT_SECONDS = 8.0
COMPENSATE_TIMEOUT_SECONDS = 4.0
REF_NAMESPACE = uuid.UUID("6f6b2a8e-3c1d-4f7a-9b2e-0d5c1e7a9f31")
CLIENT_REF_MAX = 64


class ConfirmFailed(OrderRefused):
    """ODS did not take the order: the customer's transaction rolls back. `unknown`: ODS may have created it
    (no answer, a 5xx, an unreadable answer) → compensation; a 4xx is a clear « no » (nothing created)."""

    def __init__(self, ref: Any, partner_id: Any, unknown: bool, cause: str) -> None:
        super().__init__(503, "restaurant_unreachable", retry=True)
        self.ref, self.partner_id, self.unknown, self.cause = str(ref), partner_id, unknown, cause


def pickup_order_id(customer_id: Any, client_ref: Any) -> uuid.UUID:
    """The order id of a pickup order from the customer and the cart's attempt key (idempotent retries)."""
    return uuid.uuid5(REF_NAMESPACE, f"pickup:{customer_id}:{client_ref}")


def clean_client_ref(value: Any) -> str | None:
    text = str(value or "").strip()
    return text[:CLIENT_REF_MAX] if text and all(c.isalnum() or c in "-_" for c in text) else None


async def lock_ref(session: AsyncSession, ref: Any) -> None:
    await session.execute(select(func.pg_advisory_xact_lock(func.hashtext(f"vitrine-ref:{ref}"))))


def _client(timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, transport=partner_webhooks._transport)


async def confirm(session: AsyncSession, partner: Partner, order: Order, data: dict[str, Any]) -> None:
    """Steps 1–3 above, in the customer's transaction. Raises ConfirmFailed; on success records the delivered
    confirmation (the later events follow it) and remembers it for `commit_or_compensate`."""
    await lock_ref(session, order.id)
    await session.flush()
    row = await partner_webhooks.queue(session, partner, CONFIRMED, order, data)
    if row is None:
        raise ConfirmFailed(order.id, partner.id, False, "webhook_not_configured")
    async with _client(TIMEOUT_SECONDS) as client:
        error, status, answer = await partner_webhooks.post_once(session, row, client)
    created = (
        isinstance(answer, dict) and answer.get("ok") is True and answer.get("order_id") not in (None, "")
    )
    if error is not None or not created:
        clear_no = status is not None and 400 <= status < 500
        log.warning(
            "vitrine order %s not taken by ODS: %s (status %s)", order.id, error or "not_created", status
        )
        raise ConfirmFailed(order.id, partner.id, not clear_no, error or "not_created")
    now = datetime.now(UTC)
    row.status, row.delivered_at, row.attempts, row.last_status = "delivered", now, 1, status
    order.restaurant_ack_at = now
    await session.flush()
    session.info.setdefault("vitrine_confirmed", []).append((str(order.id), partner.id))


async def commit_or_compensate(session: AsyncSession) -> None:
    """Step 4: the customer's transaction commits now; if that fails after ODS's « yes », ODS is told."""
    confirmed = session.info.pop("vitrine_confirmed", [])
    try:
        await session.commit()
    except Exception:
        log.exception("vitrine order: commit failed after ODS took it — compensating")
        await session.rollback()
        for ref, partner_id in confirmed:
            await compensate(ref, partner_id)
        raise OrderRefused(503, "restaurant_unreachable", retry=True) from None


async def on_failure(session: AsyncSession, exc: ConfirmFailed) -> None:
    """A handler caught ConfirmFailed: roll back, then compensate when ODS may have the order."""
    session.info.pop("vitrine_confirmed", None)
    await session.rollback()
    if exc.unknown:
        await compensate(exc.ref, exc.partner_id)


async def _confirmed_on_record(session: AsyncSession, ref: str) -> bool:
    try:
        oid = uuid.UUID(ref)
    except ValueError:
        return False
    return (
        await session.execute(
            select(PartnerWebhookDelivery.id)
            .where(
                PartnerWebhookDelivery.order_id == oid,
                PartnerWebhookDelivery.event == CONFIRMED,
                PartnerWebhookDelivery.status == "delivered",
            )
            .limit(1)
        )
    ).first() is not None


async def compensate(ref: str, partner_id: Any) -> bool:
    """`vitrine.order_cancelled {reason: not_confirmed}` to ODS, synchronous, own transaction, under the
    order's lock — skipped when a retry of the customer went through meanwhile. Not answered → queued."""
    from app.db import SessionLocal

    try:
        async with SessionLocal() as s, s.begin():
            await lock_ref(s, ref)
            if await _confirmed_on_record(s, ref):
                return True
            partner = await s.get(Partner, partner_id)
            if partner is None:
                return False
            row = await partner_webhooks.queue(
                s, partner, CANCELLED, None,
                {"vitrine_ref": ref, "reason": NOT_CONFIRMED, "cancelled_by": "system", "stage": "none"},
            )  # fmt: skip
            if row is None:
                return False
            async with _client(COMPENSATE_TIMEOUT_SECONDS) as client:
                error, status, _answer = await partner_webhooks.post_once(s, row, client)
            row.attempts, row.last_status, row.last_error = 1, status, error
            now = datetime.now(UTC)
            if error is None:
                row.status, row.delivered_at = "delivered", now
            else:  # the queue tries again (and drops it if a retry of the customer goes through)
                row.next_attempt_at = now + timedelta(minutes=1)
            return error is None
    except Exception:
        log.exception("vitrine order %s: compensation not sent nor queued", ref)
        return False


async def compensation_obsolete(session: AsyncSession, delivery: PartnerWebhookDelivery) -> bool:
    """The queue would send « not confirmed » again: obsolete once the customer's retry went through."""
    ref = str((delivery.payload.get("data") or {}).get("vitrine_ref") or "")
    if not ref:
        return True
    await lock_ref(session, ref)
    return await _confirmed_on_record(session, ref)
