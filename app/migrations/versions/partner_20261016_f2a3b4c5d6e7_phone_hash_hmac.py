"""partner_customer_stats.phone_hash: keyed HMAC-SHA256 instead of a bare sha256

A bare sha256 of an 8-digit Tunisian number (10^8 values) is reversed in seconds: it was a
pseudonym, not an anonymisation (manager review, 16/10/2026). The rows are a snapshot rebuilt
from `orders`, so the table is emptied and rebuilt with the keyed hash
(PARTNER_PHONE_HASH_SECRET, app/services/partner_stats.py). Downgrade rebuilds with sha256.

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-10-16 17:30:00
"""

import hashlib
import hmac
from collections.abc import Callable, Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f2a3b4c5d6e7"
down_revision: str | None = "e1f2a3b4c5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RECENT_ORDERS = 10

_PHONES = sa.text("SELECT DISTINCT contact_phone_e164 FROM orders WHERE source = 'partner'")
_COUNTS = sa.text(
    """
    SELECT count(*) AS total,
           count(*) FILTER (WHERE status = 'delivered') AS delivered,
           max(created_at) AS last_order_at
    FROM orders WHERE source = 'partner' AND contact_phone_e164 = :phone
    """
)
_RECENT = sa.text(
    """
    SELECT EXISTS (SELECT 1 FROM no_response_cases c
                   WHERE c.order_id = o.id AND c.incident_counted) AS unpaid
    FROM orders o
    WHERE o.source = 'partner' AND o.contact_phone_e164 = :phone
      AND (o.status = 'delivered'
           OR EXISTS (SELECT 1 FROM no_response_cases c WHERE c.order_id = o.id AND c.incident_counted))
    ORDER BY o.created_at DESC, o.id DESC
    LIMIT :limit
    """
)
_INSERT = sa.text(
    """
    INSERT INTO partner_customer_stats
        (phone_hash, total_orders, delivered_orders, recent_orders, recent_unpaid, last_order_at)
    VALUES (:h, :total, :delivered, :recent, :unpaid, :last)
    """
)


def _rebuild(hasher: Callable[[str], str]) -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DELETE FROM partner_customer_stats"))
    for (phone,) in conn.execute(_PHONES).all():
        if not phone:
            continue
        total, delivered, last = conn.execute(_COUNTS, {"phone": phone}).one()
        flags = [bool(u) for (u,) in conn.execute(_RECENT, {"phone": phone, "limit": RECENT_ORDERS}).all()]
        if total == 0 and not flags:
            continue
        conn.execute(
            _INSERT,
            {
                "h": hasher(phone),
                "total": int(total),
                "delivered": int(delivered),
                "recent": len(flags),
                "unpaid": sum(flags),
                "last": last,
            },
        )


def _hmac(phone: str) -> str:
    from app.config import settings

    return hmac.new(
        settings.PARTNER_PHONE_HASH_SECRET.encode(), phone.strip().encode(), hashlib.sha256
    ).hexdigest()


def _sha256(phone: str) -> str:
    return hashlib.sha256(phone.strip().encode()).hexdigest()


def upgrade() -> None:
    _rebuild(_hmac)


def downgrade() -> None:
    _rebuild(_sha256)
