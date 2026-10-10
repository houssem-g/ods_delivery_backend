"""Users (legacy User + UserProfile merged), auth tables, addresses, couriers."""

import uuid
from datetime import date, datetime, time
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import (
    E164_CHECK,
    Base,
    Point,
    app_role,
    created_at,
    legacy_id,
    package_size,
    updated_at,
    uuid_pk,
    vehicle_type,
    verification_st,
)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(f"phone_e164 {E164_CHECK}", name="phone_e164"),
        CheckConstraint("language IN ('ar','fr')", name="language"),
        Index("ix_users_role", "role"),
        Index("ix_users_referred_by_courier_id", "referred_by_courier_id"),
        Index("users_hot_deal_alerts", "id", postgresql_where=text("notify_hot_deals")),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    email: Mapped[str] = mapped_column(CITEXT, nullable=False, unique=True)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # NULL until the user (re)defines a password (imported accounts, Google-only accounts).
    password_hash: Mapped[str | None] = mapped_column(Text)
    google_sub: Mapped[str | None] = mapped_column(Text, unique=True)
    full_name: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    phone_e164: Mapped[str | None] = mapped_column(Text)
    # When phone_e164 was confirmed by a code sent to it (foreign numbers must be; +216 need not).
    # Cleared by the trigger `trg_users_phone_unverify` whenever phone_e164 changes alone.
    phone_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 'admin' replaces the legacy User.role; customer/courier mirror the legacy UserProfile.role.
    role: Mapped[str] = mapped_column(app_role, nullable=False, server_default="customer")
    language: Mapped[str] = mapped_column(Text, nullable=False, server_default="ar")
    # Existence of the legacy UserProfile (the front decides the customer side on it).
    profile_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notify_order_status: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    notify_new_orders: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    notify_incoming_orders: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    notify_chat: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    push_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    # Opt-in: a new hot deal near the default address (createHotDeal, type hot_deal_new).
    notify_hot_deals: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    whatsapp_opt_in_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    terms_accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    terms_version: Mapped[str | None] = mapped_column(Text)
    is_blacklisted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    referred_by_courier_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("couriers.id", ondelete="SET NULL", use_alter=True)
    )
    # Customer's own answer « vous vous faites livrer combien de fois par mois ? » (forecast prior).
    declared_monthly_orders: Mapped[int | None] = mapped_column(Integer)
    referred_by_code: Mapped[str | None] = mapped_column(Text)
    referred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    old_import_id: Mapped[str | None] = legacy_id()
    old_import_profile_id: Mapped[str | None] = legacy_id()
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"
    __table_args__ = (Index("ix_refresh_tokens_family", "family"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    family: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set when the token was replaced by rotation: a second tab presenting it within the
    # grace window gets a fresh token instead of triggering the reuse alarm.
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at()


class DeviceKey(Base):
    """« Se connecter avec l'empreinte / le visage »: a secret kept by the phone in its secure
    storage (Android Keystore / iOS Keychain), released only after the phone's biometric check.
    Only its sha256 is stored. Revoked with the user's sessions (new password, disabled or
    deleted account), by the user, or when a wrong secret is presented for it."""

    __tablename__ = "device_keys"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    secret_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    label: Mapped[str | None] = mapped_column(Text)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at()


class EmailCode(Base):
    __tablename__ = "email_codes"
    __table_args__ = (
        CheckConstraint("purpose IN ('verify','reset','migrate')", name="purpose"),
        Index("ix_email_codes_open", "user_id", "purpose", postgresql_where=text("used_at IS NULL")),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    code_hash: Mapped[str] = mapped_column(Text, nullable=False)
    # sha256 of the long token in the e-mailed reset link (the link works without the e-mail address).
    link_hash: Mapped[str | None] = mapped_column(Text, unique=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at()


class PhoneVerification(Base):
    """Six-digit codes sent by WhatsApp to confirm a (foreign) phone number."""

    __tablename__ = "phone_verifications"
    __table_args__ = (
        CheckConstraint(f"phone_e164 {E164_CHECK}", name="phone_e164"),
        Index("ix_phone_verifications_open", "user_id", postgresql_where=text("used_at IS NULL")),
        Index("ix_phone_verifications_phone_created_at", "phone_e164", "created_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    phone_e164: Mapped[str] = mapped_column(Text, nullable=False)
    code_hash: Mapped[str] = mapped_column(Text, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at()


class UserAddress(Base):
    __tablename__ = "user_addresses"
    __table_args__ = (
        Index("one_default_address", "user_id", unique=True, postgresql_where=text("is_default")),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    label: Mapped[str | None] = mapped_column(Text)
    # '' allowed: the profile screens save governorate/city before a street address.
    address: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    details: Mapped[str | None] = mapped_column(Text)
    governorate: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(Text)
    country: Mapped[str | None] = mapped_column(String(2), server_default="TN")
    location = mapped_column(Point())
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class Courier(Base):
    __tablename__ = "couriers"
    __table_args__ = (
        CheckConstraint(f"phone_e164 {E164_CHECK}", name="phone_e164"),
        CheckConstraint("price_per_km >= 0 AND price_per_km <= 50", name="price_per_km"),
        CheckConstraint("min_fee >= 0 AND min_fee <= 200", name="min_fee"),
        CheckConstraint("notification_radius_km BETWEEN 0 AND 100", name="notification_radius_km"),
        CheckConstraint("length(vehicle_model) <= 60", name="vehicle_model"),
        CheckConstraint("length(vehicle_plate) <= 20", name="vehicle_plate"),
        CheckConstraint("daily_goal >= 0 AND daily_goal <= 10000", name="daily_goal"),
        CheckConstraint("online_seconds >= 0", name="online_seconds"),
        Index(
            "couriers_dispatch",
            "last_location",
            postgresql_using="gist",
            postgresql_where=text("is_online AND verification = 'verified'"),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    # NULL once the account is deleted (anonymized, the row stays for the orders), or when the
    # imported profile only had a placeholder (rejected phone, re-entered at the next save).
    phone_e164: Mapped[str | None] = mapped_column(Text)
    id_document_number: Mapped[str] = mapped_column(Text, nullable=False)
    # Object key in the PRIVATE bucket; never serialized to non-admins.
    id_document_key: Mapped[str | None] = mapped_column(Text)
    vehicle: Mapped[str] = mapped_column(vehicle_type, nullable=False)
    max_package: Mapped[str] = mapped_column(package_size, nullable=False, server_default="petit")
    price_per_km: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)
    min_fee: Mapped[Decimal | None] = mapped_column(Numeric(10, 3))
    notification_radius_km: Mapped[Decimal] = mapped_column(
        Numeric(5, 1), nullable=False, server_default="10"
    )
    service_governorate: Mapped[str | None] = mapped_column(Text)
    service_city: Mapped[str | None] = mapped_column(Text)
    service_country: Mapped[str | None] = mapped_column(String(2), server_default="TN")
    service_start: Mapped[time | None] = mapped_column(Time)
    service_end: Mapped[time | None] = mapped_column(Time)
    verification: Mapped[str] = mapped_column(verification_st, nullable=False, server_default="pending")
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    referral_code: Mapped[str | None] = mapped_column(Text, unique=True)
    # Deliveries dropped after reaching the shop (cancelOrder); a counter because
    # the rule (outside a verified no-response) is not recoverable from events.
    late_cancellations: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    is_online: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    # Time online: online_since while online; online_seconds accumulated for online_day (Tunis date).
    online_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    online_day: Mapped[date | None] = mapped_column(Date)
    online_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    vehicle_model: Mapped[str | None] = mapped_column(Text)
    vehicle_plate: Mapped[str | None] = mapped_column(Text)
    daily_goal: Mapped[Decimal | None] = mapped_column(Numeric(10, 3))
    # Self-declared activity (earnings forecast, app/services/forecast.py): what he already does
    # outside the app. Optimistic by nature: the forecast discounts it and real deliveries replace it.
    declared_weekly_deliveries: Mapped[int | None] = mapped_column(Integer)
    declared_active_days: Mapped[int | None] = mapped_column(Integer)
    declared_regular_clients: Mapped[int | None] = mapped_column(Integer)
    declared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_location = mapped_column(Point())
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    old_import_id: Mapped[str | None] = legacy_id()
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


COURIER_DOCUMENT_KINDS = ("cin", "permis", "carte_grise", "assurance", "photo")
COURIER_DOCUMENT_STATUSES = ("pending", "verified", "rejected")


class CourierDocument(Base):
    """A courier's document (private upload), reviewed by an admin. One row per kind: a new
    upload replaces the file and sends it back to review."""

    __tablename__ = "courier_documents"
    __table_args__ = (
        UniqueConstraint("courier_id", "kind"),
        CheckConstraint("kind IN (" + ",".join(f"'{k}'" for k in COURIER_DOCUMENT_KINDS) + ")", name="kind"),
        CheckConstraint(
            "status IN (" + ",".join(f"'{k}'" for k in COURIER_DOCUMENT_STATUSES) + ")", name="status"
        ),
        CheckConstraint("length(note) <= 500", name="note"),
        Index("ix_courier_documents_status_created_at", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    courier_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("couriers.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    file_key: Mapped[str] = mapped_column(Text, nullable=False)
    expires_on: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    note: Mapped[str | None] = mapped_column(Text)
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()
