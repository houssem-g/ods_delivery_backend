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
    Numeric,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, created_at, updated_at, uuid_pk

LEDGER_KINDS = (
    "commission_due", "commission_waived_launch", "commission_waived_quota", "payment_received", "adjustment",
)  # fmt: skip


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
