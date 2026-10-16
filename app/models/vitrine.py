"""Vitrine (étape 8, 16/10/2026): a shop with its own catalog (categories, products, prices),
created by an admin only, fed by hand (`manual`) or by the ODS restaurant it is linked to (`ods`,
the partner pushes a full snapshot to PUT /api/partner/v1/catalog). Contract:
ods-audit/livraison-1610/vitrine/CONTRAT-A.md. Nothing is ever deleted physically: archived."""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, created_at, updated_at, uuid_pk

VITRINE_STATUSES = ("draft", "active", "paused", "archived")
CATALOG_SOURCES = ("manual", "ods")
SHOP_VISIBILITIES = ("public", "qa")
MAX_PRODUCT_PRICE = Decimal("2000")


def _in(values: tuple[str, ...]) -> str:
    return ",".join(f"'{v}'" for v in values)


class Vitrine(Base):
    """One per shop (`shop_id` is the key). `partner_id` = the ODS restaurant feeding the catalog."""

    __tablename__ = "vitrines"
    __table_args__ = (
        CheckConstraint(f"status IN ({_in(VITRINE_STATUSES)})", name="status"),
        CheckConstraint(f"source IN ({_in(CATALOG_SOURCES)})", name="source"),
        CheckConstraint("source <> 'ods' OR partner_id IS NOT NULL", name="ods_has_partner"),
        CheckConstraint("merchant_link_no >= 0 AND merchant_link_opens >= 0", name="merchant_link"),
        Index("ix_vitrines_status", "status"),
    )

    shop_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("shops.id", ondelete="RESTRICT"), primary_key=True
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="draft")
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default="manual")
    partner_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("partners.id", ondelete="RESTRICT"), unique=True
    )
    catalog_version: Mapped[str | None] = mapped_column(Text)
    catalog_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    catalog_test: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    # {tz, weekly: [{days: [1..7], open: "HH:MM", close: "HH:MM"}]}; NULL = unknown (ordering allowed).
    hours: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    # Merchant's private link (/m/<token>): sha256 to find it, Fernet copy to send it again.
    merchant_token_hash: Mapped[str | None] = mapped_column(Text, unique=True)
    merchant_token_enc: Mapped[str | None] = mapped_column(Text)
    merchant_link_no: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    merchant_link_opens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    merchant_link_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When the hand-made catalog was archived by a switch to ODS (back to it within 30 days).
    manual_archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class VitrineCategory(Base):
    __tablename__ = "vitrine_categories"
    __table_args__ = (
        CheckConstraint("length(name) BETWEEN 1 AND 60", name="name"),
        Index(
            "vitrine_categories_ods_id",
            "shop_id",
            "ods_category_id",
            unique=True,
            postgresql_where=text("ods_category_id IS NOT NULL"),
        ),
        Index("ix_vitrine_categories_shop_id", "shop_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    shop_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vitrines.shop_id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    ods_category_id: Mapped[int | None] = mapped_column(BigInteger)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class VitrineProduct(Base):
    __tablename__ = "vitrine_products"
    __table_args__ = (
        CheckConstraint("length(name) BETWEEN 1 AND 100", name="name"),
        CheckConstraint("length(description) <= 300", name="description"),
        CheckConstraint(f"price >= 0 AND price <= {MAX_PRODUCT_PRICE}", name="price"),
        Index(
            "vitrine_products_ods_id",
            "shop_id",
            "ods_menu_id",
            unique=True,
            postgresql_where=text("ods_menu_id IS NOT NULL"),
        ),
        Index("ix_vitrine_products_shop_id", "shop_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    shop_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vitrines.shop_id", ondelete="CASCADE"), nullable=False
    )
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vitrine_categories.id", ondelete="SET NULL")
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    price: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)
    # One of our public uploads (key) or an https URL (ODS photos).
    photo_key: Mapped[str | None] = mapped_column(Text)
    available: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    position: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    ods_menu_id: Mapped[int | None] = mapped_column(BigInteger)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class VitrinePoster(Base):
    """A printed poster (A5, QR to /v/<slug>?src=<code>): its scans are counted per code."""

    __tablename__ = "vitrine_posters"
    __table_args__ = (
        PrimaryKeyConstraint("shop_id", "code"),
        CheckConstraint("length(label) BETWEEN 1 AND 40", name="label"),
        CheckConstraint("code ~ '^[a-z0-9-]{1,32}$'", name="code"),
    )

    shop_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vitrines.shop_id", ondelete="CASCADE"), nullable=False
    )
    code: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at()


class PartnerCatalog(Base):
    """The last snapshot an ODS restaurant pushed (kept even without a Vitrine: the admin sees
    « 24 produits reçus » before linking). `content_hash` = Delivery's own sha256 of the content."""

    __tablename__ = "partner_catalogs"

    partner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("partners.id", ondelete="CASCADE"), primary_key=True
    )
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    catalog_version: Mapped[str | None] = mapped_column(Text)
    test: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    products_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    # Last push seen (even unchanged), refreshed at most hourly: « ODS est joignable ».
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class VitrineStatDaily(Base):
    __tablename__ = "vitrine_stats_daily"
    __table_args__ = (
        PrimaryKeyConstraint("shop_id", "day"),
        CheckConstraint("views >= 0", name="views"),
    )

    shop_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vitrines.shop_id", ondelete="CASCADE"), nullable=False
    )
    day: Mapped[date] = mapped_column(Date, nullable=False)
    views: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    scans: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))


class VitrineVisit(Base):
    """One device, one day, one kind ('view' or 'scan:<code>'): counted once. Purged after 40 days."""

    __tablename__ = "vitrine_visits"
    __table_args__ = (
        PrimaryKeyConstraint("shop_id", "day", "kind", "visitor"),
        Index("ix_vitrine_visits_day", "day"),
    )

    shop_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vitrines.shop_id", ondelete="CASCADE"), nullable=False
    )
    day: Mapped[date] = mapped_column(Date, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    visitor: Mapped[str] = mapped_column(Text, nullable=False)
