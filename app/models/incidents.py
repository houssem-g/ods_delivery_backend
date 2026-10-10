"""'Client ne répond pas' cases and hot deals (ex-ResaleOrder)."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, Numeric, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Point, created_at, legacy_id, updated_at, uuid_pk

NO_RESPONSE_RESOLUTIONS = (
    "customer_confirmed", "courier_reached", "resold", "cancelled_kept", "returned_to_shop",
    "auto_closed", "courier_cancelled_other", "delivered", "order_cancelled", "realerted",
)  # fmt: skip


class NoResponseCase(Base):
    __tablename__ = "no_response_cases"
    __table_args__ = (
        CheckConstraint("status IN ('waiting','expired','resolved')", name="status"),
        CheckConstraint(
            "resolution IN (" + ",".join(f"'{r}'" for r in NO_RESPONSE_RESOLUTIONS) + ")", name="resolution"
        ),
        Index(
            "one_open_case_per_order", "order_id", unique=True, postgresql_where=text("status = 'waiting'")
        ),
        Index("due_cases", "deadline_at", postgresql_where=text("status = 'waiting'")),
        Index("ix_no_response_cases_order_id", "order_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    courier_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("couriers.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    purchase_amount: Mapped[Decimal | None] = mapped_column(Numeric(10, 3))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    final_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution: Mapped[str | None] = mapped_column(Text)
    incident_counted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    customer_answered_late: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    # {"in_app": bool, "push_devices": int, "whatsapp": str, "sms": str}
    channels: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    messaging_status: Mapped[str | None] = mapped_column(Text)
    old_import_id: Mapped[str | None] = legacy_id()
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


def _same_as_price(context: Any) -> Any:
    """start_price / floor_price not given (imports, older writers): the price, i.e. no decay."""
    return context.get_current_parameters()["price"]


class HotDeal(Base):
    __tablename__ = "hot_deals"
    __table_args__ = (
        CheckConstraint("purchase_amount >= 0", name="purchase_amount"),
        CheckConstraint("discount_percentage BETWEEN 0 AND 100", name="discount_percentage"),
        CheckConstraint("price >= 0", name="price"),
        CheckConstraint("floor_price >= 0 AND floor_price <= start_price", name="floor_price"),
        CheckConstraint("drop_step >= 0 AND drop_step <= 100", name="drop_step"),
        CheckConstraint("drop_every_min BETWEEN 1 AND 1440", name="drop_every_min"),
        CheckConstraint("status IN ('available','sold','expired')", name="status"),
        CheckConstraint("status <> 'sold' OR buyer_id IS NOT NULL", name="sold_has_buyer"),
        Index(
            "hot_deals_available",
            "pickup_location",
            postgresql_using="gist",
            postgresql_where=text("status = 'available'"),
        ),
        Index("ix_hot_deals_original_order_id", "original_order_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    original_order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False
    )
    courier_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("couriers.id", ondelete="CASCADE"), nullable=False
    )
    items_text: Mapped[str] = mapped_column(Text, nullable=False)
    shop_name: Mapped[str | None] = mapped_column(Text)
    shop_address: Mapped[str | None] = mapped_column(Text)
    purchase_amount: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)
    discount_percentage: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)
    # Price decay: current = max(floor, start - floor(minutes listed / drop_every_min) * drop_step)
    # (app/services/hot_deals.current_price). price = start_price.
    start_price: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False, default=_same_as_price)
    floor_price: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False, default=_same_as_price)
    drop_step: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False, server_default="0.500")
    drop_every_min: Mapped[int] = mapped_column(Integer, nullable=False, server_default="5")
    include_delivery: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    delivery_fee: Mapped[Decimal | None] = mapped_column(Numeric(10, 3))
    photo_key: Mapped[str | None] = mapped_column(Text)
    pickup_location = mapped_column(Point())
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="available")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    buyer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    reserved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    buyer_order_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="SET NULL")
    )
    old_import_id: Mapped[str | None] = legacy_id()
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()
