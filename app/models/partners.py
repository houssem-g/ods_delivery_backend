"""Partners (ODS restaurants calling /api/partner/v1), their customer badge per phone number and
their outgoing webhook queue. See docs: ods-audit/livraison-1610/lot2/2A-CONTRAT.md."""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, Numeric, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, created_at, updated_at, uuid_pk

PARTNER_KINDS = ("ods_restaurant",)
ORDER_SOURCES = ("app", "partner")
RETURN_OUTCOMES = ("accepted", "refused", "shop_closed")
WEBHOOK_STATUSES = ("pending", "delivered", "failed")
MAX_OPEN_ORDERS_LIMIT = 500


class Partner(Base):
    """A restaurant ODS with its own API key. `technical_user_id` is the Delivery customer account
    (role customer, no password) that owns the partner's orders, so place_order / accept_offer /
    cancellation keep working unchanged."""

    __tablename__ = "partners"
    __table_args__ = (
        CheckConstraint("kind IN (" + ",".join(f"'{k}'" for k in PARTNER_KINDS) + ")", name="kind"),
        CheckConstraint("length(name) BETWEEN 1 AND 120", name="name"),
        CheckConstraint(f"max_open_orders BETWEEN 1 AND {MAX_OPEN_ORDERS_LIMIT}", name="max_open_orders"),
        CheckConstraint("returns_refused >= 0", name="returns_refused"),
        CheckConstraint("debt_amount >= 0", name="debt_amount"),
        Index("ix_partners_ods_restaurant_id", "ods_restaurant_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False, server_default="ods_restaurant")
    ods_restaurant_id: Mapped[int | None] = mapped_column(Integer)
    # The key itself is shown once; only its sha256 is kept. key_prefix identifies it to humans.
    key_prefix: Mapped[str] = mapped_column(Text, nullable=False)
    key_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    technical_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    webhook_url: Mapped[str | None] = mapped_column(Text)
    webhook_secret: Mapped[str | None] = mapped_column(Text)
    max_open_orders: Mapped[int] = mapped_column(Integer, nullable=False, server_default="50")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # D5 (owner, 16/10/2026): « ce restaurant a refusé X retours », shown to the couriers asked.
    returns_refused: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # D4: the restaurant was closed when the courier brought the meal back: advance + delivery fee
    # owed, paid at the next opening; no new order while it runs.
    debt_amount: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False, server_default="0")
    debt_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class PartnerCustomerStat(Base):
    """The « n'a pas payé X fois sur ses Y dernières commandes » record of a phone number over the
    partner orders (owner, 10/10 and 16/10/2026). Keyed by sha256 of the E.164 number so it survives
    the anonymisation of old orders; refreshed from `orders` on every terminal status."""

    __tablename__ = "partner_customer_stats"

    phone_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    total_orders: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    delivered_orders: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # The last 10 finished orders (delivered, or left with a counted no-response incident).
    recent_orders: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    recent_unpaid: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_order_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = updated_at()


class PartnerWebhookDelivery(Base):
    """One outgoing event to a partner's webhook URL (copy of the ODS model): first attempt within
    30 s, then 1, 2, 4, 8 and 16 minutes; the same id is sent at every attempt (receiver idempotence)."""

    __tablename__ = "partner_webhook_deliveries"
    __table_args__ = (
        CheckConstraint("status IN (" + ",".join(f"'{s}'" for s in WEBHOOK_STATUSES) + ")", name="status"),
        CheckConstraint("attempts >= 0", name="attempts"),
        Index(
            "partner_webhook_deliveries_due",
            "next_attempt_at",
            postgresql_where=text("status = 'pending'"),
        ),
        Index("ix_partner_webhook_deliveries_partner_id_created_at", "partner_id", "created_at"),
        Index("ix_partner_webhook_deliveries_order_id", "order_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    partner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("partners.id", ondelete="CASCADE"), nullable=False
    )
    order_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="SET NULL")
    )
    event: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at()
