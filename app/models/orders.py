"""Orders and everything hanging off an order (stops, events, offers, tracking, issues, ratings, chat)."""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import (
    E164_CHECK,
    Base,
    Point,
    created_at,
    legacy_id,
    offer_status,
    order_status,
    package_size,
    updated_at,
    uuid_pk,
)

COURIER_REQUIRED_STATUSES = ",".join(
    f"'{status}'"
    for status in (
        "accepted", "at_shop", "price_confirmation_needed", "purchased", "on_the_way", "delivered",
        "client_no_response",
    )
)  # fmt: skip


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint("length(items_text) BETWEEN 1 AND 2000", name="items_text"),
        CheckConstraint("quantity BETWEEN 1 AND 100", name="quantity"),
        CheckConstraint("estimated_price >= 0", name="estimated_price"),
        CheckConstraint(f"contact_phone_e164 {E164_CHECK}", name="contact_phone_e164"),
        CheckConstraint("delivery_fee >= 0 AND delivery_fee <= 200", name="delivery_fee"),
        CheckConstraint("purchase_amount >= 0 AND purchase_amount <= 2000", name="purchase_amount"),
        CheckConstraint("payment_method IN ('cash')", name="payment_method"),
        CheckConstraint("cancelled_by IN ('customer','courier','admin','system')", name="cancelled_by"),
        CheckConstraint(
            f"status NOT IN ({COURIER_REQUIRED_STATUSES}) OR courier_id IS NOT NULL",
            name="courier_when_assigned",
        ),
        CheckConstraint("status <> 'delivered' OR delivered_at IS NOT NULL", name="delivered_at"),
        CheckConstraint("status <> 'cancelled' OR cancelled_at IS NOT NULL", name="cancelled_at"),
        Index("orders_customer", "customer_id", text("created_at DESC")),
        Index(
            "orders_courier",
            "courier_id",
            "status",
            text("created_at DESC"),
            postgresql_where=text("courier_id IS NOT NULL"),
        ),
        Index(
            "orders_open_geo",
            "delivery_location",
            postgresql_using="gist",
            postgresql_where=text("status IN ('pending','offers_received')"),
        ),
        Index("orders_active", "status", postgresql_where=text("status NOT IN ('delivered','cancelled')")),
        Index("ix_orders_preferred_courier_id", "preferred_courier_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    courier_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("couriers.id", ondelete="RESTRICT")
    )
    preferred_courier_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("couriers.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(order_status, nullable=False, server_default="pending")
    items_text: Mapped[str] = mapped_column(Text, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    notes: Mapped[str | None] = mapped_column(Text)
    alternatives: Mapped[str | None] = mapped_column(Text)
    package: Mapped[str] = mapped_column(package_size, nullable=False, server_default="petit")
    estimated_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 3))
    # Delivery snapshot: the order keeps the contact/address it was placed with.
    contact_name: Mapped[str] = mapped_column(Text, nullable=False)
    contact_phone_e164: Mapped[str | None] = mapped_column(Text)
    delivery_address: Mapped[str] = mapped_column(Text, nullable=False)
    delivery_details: Mapped[str | None] = mapped_column(Text)
    delivery_governorate: Mapped[str | None] = mapped_column(Text)
    delivery_city: Mapped[str | None] = mapped_column(Text)
    delivery_location = mapped_column(Point())
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    distance_km: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    eta_minutes: Mapped[int | None] = mapped_column(Integer)
    # Total paid at the shops (what the customer reimburses); per-stop amounts live on order_stops.
    purchase_amount: Mapped[Decimal | None] = mapped_column(Numeric(10, 3))
    delivery_fee: Mapped[Decimal | None] = mapped_column(Numeric(10, 3))
    payment_method: Mapped[str] = mapped_column(Text, nullable=False, server_default="cash")
    price_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Legacy Order.current_shop_index: the stop the courier is working on (0-based seq).
    current_stop_seq: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0")
    cancelled_by: Mapped[str | None] = mapped_column(Text)
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resale_deal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hot_deals.id", ondelete="SET NULL", use_alter=True)
    )
    legacy_b44_id: Mapped[str | None] = legacy_id()
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class OrderStop(Base):
    __tablename__ = "order_stops"
    __table_args__ = (
        UniqueConstraint("order_id", "seq"),
        CheckConstraint("status IN ('pending','en_route','at_shop','purchased','skipped')", name="status"),
        CheckConstraint("purchase_amount >= 0 AND purchase_amount <= 2000", name="purchase_amount"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    shop_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("shops.id", ondelete="SET NULL")
    )
    place_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("places.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(Text, nullable=False)
    address: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    governorate: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(Text)
    location = mapped_column(Point())
    items: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    purchase_amount: Mapped[Decimal | None] = mapped_column(Numeric(10, 3))
    receipt_key: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class OrderStatusEvent(Base):
    """Append-only; written in the same transaction as orders.status."""

    __tablename__ = "order_status_events"
    __table_args__ = (Index("ix_order_status_events_order_id_created_at", "order_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    from_status: Mapped[str | None] = mapped_column(order_status)
    to_status: Mapped[str] = mapped_column(order_status, nullable=False)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    cancelled_by: Mapped[str | None] = mapped_column(Text)
    location = mapped_column(Point())
    created_at: Mapped[datetime] = created_at()


class OrderOffer(Base):
    __tablename__ = "order_offers"
    __table_args__ = (
        CheckConstraint("proposed_fee > 0 AND proposed_fee <= 200", name="proposed_fee"),
        CheckConstraint("eta_minutes BETWEEN 0 AND 600", name="eta_minutes"),
        CheckConstraint("length(message) <= 500", name="message"),
        Index(
            "one_live_offer_per_courier",
            "order_id",
            "courier_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        Index("one_accepted_offer", "order_id", unique=True, postgresql_where=text("status = 'accepted'")),
        Index("ix_order_offers_courier_status_created", "courier_id", "status", text("created_at DESC")),
        Index("ix_order_offers_order_id", "order_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    courier_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("couriers.id", ondelete="CASCADE"), nullable=False
    )
    proposed_fee: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)
    eta_minutes: Mapped[int | None] = mapped_column(Integer)
    distance_km: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    message: Mapped[str | None] = mapped_column(Text)
    courier_rating_snapshot: Mapped[Decimal | None] = mapped_column(Numeric(3, 2))
    status: Mapped[str] = mapped_column(offer_status, nullable=False, server_default="pending")
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    legacy_b44_id: Mapped[str | None] = legacy_id()
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class OrderTracking(Base):
    """Live courier position per order (frequent updates, short rows)."""

    __tablename__ = "order_tracking"

    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), primary_key=True
    )
    courier_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("couriers.id"), nullable=False
    )
    location = mapped_column(Point(), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OrderIssue(Base):
    __tablename__ = "order_issues"
    __table_args__ = (Index("ix_order_issues_order_id", "order_id"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    reporter_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    issue_type: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    photo_key: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = created_at()


class OrderRating(Base):
    __tablename__ = "order_ratings"
    __table_args__ = (
        CheckConstraint("rating BETWEEN 1 AND 5", name="rating"),
        Index("ix_order_ratings_courier_id", "courier_id"),
    )

    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), primary_key=True
    )
    courier_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("couriers.id", ondelete="CASCADE"), nullable=False
    )
    rater_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    rating: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at()


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint("sender_role IN ('customer','courier')", name="sender_role"),
        CheckConstraint("length(body) BETWEEN 1 AND 1000", name="body"),
        Index("ix_messages_order_id_created_at", "order_id", "created_at"),
        Index("messages_unread", "recipient_id", postgresql_where=text("read_at IS NULL")),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    sender_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    recipient_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    sender_role: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    is_template: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    legacy_b44_id: Mapped[str | None] = legacy_id()
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class OrderDraft(Base):
    """An unfinished NewOrder form, kept 24 h after its last save (server side: it survives
    a reinstall or a change of device)."""

    __tablename__ = "order_drafts"
    __table_args__ = (
        CheckConstraint("length(title) <= 120", name="title"),
        Index("ix_order_drafts_user_id_expires_at", "user_id", "expires_at"),
        Index("ix_order_drafts_expires_at", "expires_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    created_at: Mapped[datetime] = created_at()
    # Written by the service (no trigger): expires_at = updated_at + 24 h.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
