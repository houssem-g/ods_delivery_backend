"""In-app notifications, device tokens, push delivery log, WhatsApp/SMS log."""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, created_at, legacy_id, updated_at, uuid_pk

NOTIFICATION_TYPES = (
    "order_accepted", "at_shop", "purchased", "on_the_way", "delivered", "order_cancelled",
    "delivery_cancelled", "delivery_delayed", "eta_update", "new_order", "new_offer", "new_message",
    "emergency_contact", "customer_responded", "customer_no_response_final", "order_confirmed",
    "order_preparing", "hot_deal_reserved", "issue_reported", "account_verified", "account_rejected",
)  # fmt: skip
# Legacy synonyms merged at import time and when a caller still sends them.
NOTIFICATION_TYPE_SYNONYMS = {
    "message": "new_message",
    "order_delivered": "delivered",
    "courier_on_way": "on_the_way",
    "incoming_order": "new_order",
}


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        CheckConstraint("type IN (" + ",".join(f"'{t}'" for t in NOTIFICATION_TYPES) + ")", name="type"),
        Index("ix_notifications_user_id_created_at", "user_id", text("created_at DESC")),
        Index("notifications_unread", "user_id", postgresql_where=text("read_at IS NULL")),
        Index("ix_notifications_order_id", "order_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    order_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="SET NULL")
    )
    type: Mapped[str] = mapped_column(Text, nullable=False)
    title_ar: Mapped[str | None] = mapped_column(Text)
    title_fr: Mapped[str | None] = mapped_column(Text)
    body_ar: Mapped[str | None] = mapped_column(Text)
    body_fr: Mapped[str | None] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    legacy_b44_id: Mapped[str | None] = legacy_id()
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class DeviceToken(Base):
    __tablename__ = "device_tokens"
    __table_args__ = (
        CheckConstraint("platform IN ('web','android','ios')", name="platform"),
        Index("ix_device_tokens_active_user", "user_id", postgresql_where=text("is_active")),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    app_version: Mapped[str | None] = mapped_column(Text)
    device_model: Mapped[str | None] = mapped_column(Text)
    locale: Mapped[str | None] = mapped_column(Text)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    legacy_b44_id: Mapped[str | None] = legacy_id()
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class PushDelivery(Base):
    """One row per push attempt and device (both providers): delivery audit and the test oracle."""

    __tablename__ = "push_deliveries"
    __table_args__ = (
        CheckConstraint("provider IN ('fcm','log')", name="provider"),
        CheckConstraint("status IN ('sent','failed','invalid_token')", name="status"),
        Index("ix_push_deliveries_user_id_created_at", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    device_token_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("device_tokens.id", ondelete="SET NULL")
    )
    notification_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("notifications.id", ondelete="SET NULL")
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = created_at()


class OutboundMessage(Base):
    """ex-MessageLog: WhatsApp / SMS sends and their delivery receipts."""

    __tablename__ = "outbound_messages"
    __table_args__ = (
        CheckConstraint("channel IN ('whatsapp','sms')", name="channel"),
        Index(
            "ix_outbound_messages_due",
            "next_attempt_at",
            postgresql_where=text("status IN ('retry_pending','queued')"),
        ),
        Index("ix_outbound_messages_to_e164_created_at", "to_e164", "created_at"),
        Index("ix_outbound_messages_provider_message_id", "provider_message_id"),
        Index("ix_outbound_messages_order_id", "order_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    template_name: Mapped[str | None] = mapped_column(Text)
    lang: Mapped[str | None] = mapped_column(Text)
    params: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")
    to_e164: Mapped[str] = mapped_column(Text, nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    order_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="SET NULL")
    )
    notification_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("notifications.id", ondelete="SET NULL")
    )
    idempotency_key: Mapped[str | None] = mapped_column(Text, unique=True)
    critical: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    status: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str | None] = mapped_column(Text)
    provider_message_id: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    error_code: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fallback_deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fallback_status: Mapped[str | None] = mapped_column(Text)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("outbound_messages.id")
    )
    legacy_b44_id: Mapped[str | None] = legacy_id()
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()
