"""Money (ledger, statements), settings, audit log, uploaded files."""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, created_at, updated_at, uuid_pk

LEDGER_KINDS = (
    "commission_due", "commission_waived_launch", "commission_waived_quota", "payment_received", "adjustment",
    "credit_topup", "credit_bonus", "credit_prime",
)  # fmt: skip
CREDIT_TOPUP_METHODS = ("bank_deposit", "cashier")
CREDIT_TOPUP_STATUSES = ("pending", "approved", "rejected")


class CourierStatement(Base):
    __tablename__ = "courier_statements"
    __table_args__ = (
        UniqueConstraint("courier_id", "period_start"),
        CheckConstraint("status IN ('open','sent','paid','void')", name="status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    courier_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("couriers.id"), nullable=False
    )
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    total_due: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at()


class CourierLedgerEntry(Base):
    __tablename__ = "courier_ledger_entries"
    __table_args__ = (
        UniqueConstraint("order_id", "kind"),
        CheckConstraint("kind IN (" + ",".join(f"'{k}'" for k in LEDGER_KINDS) + ")", name="kind"),
        Index("ix_courier_ledger_entries_courier_id_created_at", "courier_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    courier_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("couriers.id", ondelete="RESTRICT"), nullable=False
    )
    order_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="RESTRICT")
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    # Positive = owed to ODS; negative = paid / credit. Waived commissions keep their nominal amount.
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)
    statement_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("courier_statements.id", use_alter=True)
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = created_at()


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_log_entity_entity_id_created_at", "entity", "entity_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(Text, nullable=False)
    entity: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[str] = mapped_column(Text, nullable=False)
    before: Mapped[dict | None] = mapped_column(JSONB)
    after: Mapped[dict | None] = mapped_column(JSONB)
    ip: Mapped[str | None] = mapped_column(INET)
    created_at: Mapped[datetime] = created_at()


class File(Base):
    """An uploaded object. `key` is the bucket key: public/… or private/…."""

    __tablename__ = "files"
    __table_args__ = (
        CheckConstraint("visibility IN ('public','private')", name="visibility"),
        CheckConstraint("size_bytes >= 0", name="size_bytes"),
        Index("ix_files_owner_id", "owner_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    visibility: Mapped[str] = mapped_column(Text, nullable=False)
    purpose: Mapped[str] = mapped_column(Text, nullable=False, server_default="generic")
    content_type: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    original_name: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class TranslationUsage(Base):
    """The chat translation spend per month (UTC; first day of the month), checked against
    TRANSLATE_MONTHLY_BUDGET_USD before every call."""

    __tablename__ = "translation_usage"

    month: Mapped[date] = mapped_column(Date, primary_key=True)
    calls: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    output_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False, server_default=text("0"))


class CreditTopup(Base):
    """A courier's prepaid-credit top-up (decision D-10): a cash deposit at a bank counter on the ODS
    account (receipt photo, approved by an admin) or cash handed to a cashier (approved at once).
    The money itself lives in courier_ledger_entries (credit_topup / credit_bonus, negative amounts)."""

    __tablename__ = "credit_topups"
    __table_args__ = (
        CheckConstraint("method IN (" + ",".join(f"'{m}'" for m in CREDIT_TOPUP_METHODS) + ")", name="method"),
        CheckConstraint("status IN (" + ",".join(f"'{s}'" for s in CREDIT_TOPUP_STATUSES) + ")", name="status"),
        CheckConstraint("amount > 0 AND amount <= 1000", name="amount"),
        CheckConstraint("bonus >= 0", name="bonus"),
        CheckConstraint("method <> 'cashier' OR cashier_user_id IS NOT NULL", name="cashier"),
        Index("ix_credit_topups_courier_id_created_at", "courier_id", "created_at"),
        Index("credit_topups_pending", "created_at", postgresql_where=text("status = 'pending'")),
        Index(
            "credit_topups_unremitted",
            "cashier_user_id",
            postgresql_where=text("method = 'cashier' AND status = 'approved' AND remitted_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    courier_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("couriers.id", ondelete="RESTRICT"), nullable=False
    )
    method: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    # Cash the courier paid (DT); the bonus is added on approval (TOPUP_BONUS in app/services/credit.py).
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)
    bonus: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False, server_default="0")
    # bank_deposit: the receipt photo (PRIVATE bucket, signed for admins only) and the reference he typed.
    receipt_key: Mapped[str | None] = mapped_column(Text)
    reference: Mapped[str | None] = mapped_column(Text)
    cashier_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT")
    )
    note: Mapped[str | None] = mapped_column(Text)
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # cashier: when the cashier handed this cash over to ODS (set by an admin).
    remitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at()


class CreditCashier(Base):
    """A user allowed to sell prepaid credit for cash (the ODS café in Sousse): one row per user."""

    __tablename__ = "credit_cashiers"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    label: Mapped[str] = mapped_column(Text, nullable=False)
    address: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = created_at()


class CourierClientEstimate(Base):
    """The courier's own estimate of how often one of his invited clients orders (per month)."""

    __tablename__ = "courier_client_estimates"
    __table_args__ = (CheckConstraint("monthly_orders BETWEEN 0 AND 60", name="monthly_orders"),)

    courier_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("couriers.id", ondelete="CASCADE"), primary_key=True
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    monthly_orders: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = updated_at()


class EarningsForecast(Base):
    """A forecast snapshot (app/services/forecast.py). kind 'week' (the next 7 days, written every
    night) is compared with what really happened once the week is over (actual_*): that comparison
    is what corrects the next forecasts. kind 'month' is the snapshot shown in the app."""

    __tablename__ = "earnings_forecasts"
    __table_args__ = (
        UniqueConstraint("courier_id", "kind", "period_start", "as_of"),
        CheckConstraint("kind IN ('week','month')", name="kind"),
        Index("earnings_forecasts_to_evaluate", "period_end", postgresql_where=text("evaluated_at IS NULL")),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    courier_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("couriers.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    as_of: Mapped[date] = mapped_column(Date, nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)  # inclusive
    # Gross delivery fees forecast for the period (the part not yet earned), DT.
    p25: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)
    p50: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)
    p75: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)
    expected_deliveries: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)
    method: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    actual_fees: Mapped[Decimal | None] = mapped_column(Numeric(10, 3))
    actual_deliveries: Mapped[int | None] = mapped_column(Integer)
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at()


class ForecastFactor(Base):
    """What the forecast learned across all couriers (weekday, holiday, Ramadan, rain, overall bias):
    value = multiplicative factor, weight = how much evidence backs it."""

    __tablename__ = "forecast_factors"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    weight: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False, server_default="0")
    updated_at: Mapped[datetime] = updated_at()
