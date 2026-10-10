"""Places (OSM cache, ex-PlaceIndex), shops, menu items and reviews."""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Double,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Point, created_at, legacy_id, updated_at, uuid_pk


class Place(Base):
    __tablename__ = "places"
    __table_args__ = (
        Index("places_geo", "location", postgresql_using="gist"),
        Index(
            "places_name", "name_norm", postgresql_using="gin", postgresql_ops={"name_norm": "gin_trgm_ops"}
        ),
        Index("places_cat", "category"),
        Index(
            "places_search",
            "search_norm",
            postgresql_using="gin",
            postgresql_ops={"search_norm": "gin_trgm_ops"},
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    osm_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    name_norm: Mapped[str] = mapped_column(Text, nullable=False)
    # Normalized name + address + city (app.services.text_norm.search_text):
    # the haystack of the text scores, searched with LIKE '%token%' (trigram index).
    search_norm: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    category: Mapped[str] = mapped_column(Text, nullable=False)
    address: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(Text)
    governorate: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    opening_hours: Mapped[str | None] = mapped_column(Text)
    location = mapped_column(Point(), nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default="osm")
    source_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Search ranking (searchByBbox sorts on it): percent, 0-100 (the legacy shape answers 0-1).
    quality_score: Mapped[int | None] = mapped_column(SmallInteger)
    refreshed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    old_import_id: Mapped[str | None] = legacy_id()
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class Shop(Base):
    __tablename__ = "shops"
    __table_args__ = (
        CheckConstraint("review_status IN ('pending','approved','rejected')", name="review_status"),
        Index("shops_geo", "location", postgresql_using="gist"),
        Index("ix_shops_review_status", "review_status"),
        Index("ix_shops_proposed_by", "proposed_by"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    place_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("places.id", ondelete="SET NULL"))
    # Legacy Shop.osm_id: an OSM id or 'custom_…' for proposals; matched by shop reviews.
    osm_id: Mapped[str | None] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    address: Mapped[str | None] = mapped_column(Text)
    governorate: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    categories: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")
    opening_hours: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    photo_key: Mapped[str | None] = mapped_column(Text)
    location = mapped_column(Point(), nullable=False)
    review_status: Mapped[str] = mapped_column(Text, nullable=False, server_default="approved")
    proposed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    proposed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    old_import_id: Mapped[str | None] = legacy_id()
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class ShopMenuItem(Base):
    __tablename__ = "shop_menu_items"
    __table_args__ = (CheckConstraint("price >= 0", name="price"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    shop_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("shops.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    price: Mapped[Decimal | None] = mapped_column(Numeric(10, 3))
    description: Mapped[str | None] = mapped_column(Text)
    photo_key: Mapped[str | None] = mapped_column(Text)
    position: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")


class ShopReview(Base):
    __tablename__ = "shop_reviews"
    __table_args__ = (
        CheckConstraint("rating BETWEEN 1 AND 5", name="rating"),
        # A review may be about a place we can't resolve (the front keys some by name + position).
        CheckConstraint("num_nonnulls(shop_id, place_id) <= 1", name="one_target"),
        Index("one_review_per_user_target", "user_id", "target_key", unique=True),
        Index("ix_shop_reviews_target_key", "target_key"),
        Index(
            "one_review_per_user_shop",
            "user_id",
            "shop_id",
            unique=True,
            postgresql_where=text("shop_id IS NOT NULL"),
        ),
        Index(
            "one_review_per_user_place",
            "user_id",
            "place_id",
            unique=True,
            postgresql_where=text("place_id IS NOT NULL"),
        ),
        Index("ix_shop_reviews_shop_id", "shop_id"),
        Index("ix_shop_reviews_place_id", "place_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    shop_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("shops.id", ondelete="CASCADE")
    )
    place_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("places.id", ondelete="CASCADE"))
    # Legacy ShopReview.shop_osm_id as the front sends it ('shop:<id>', an OSM id,
    # 'place:<name>@<lat>,<lng>'); reads filter on it.
    target_key: Mapped[str] = mapped_column(Text, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    rating: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text)
    photo_keys: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")
    old_import_id: Mapped[str | None] = legacy_id()
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class GeocodeCache(Base):
    """Nominatim answers (hits and misses), as its usage policy asks: one row per normalized query."""

    __tablename__ = "geocode_cache"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    found: Mapped[bool] = mapped_column(Boolean, nullable=False)
    lat: Mapped[float | None] = mapped_column(Double)
    lng: Mapped[float | None] = mapped_column(Double)
    result: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = created_at()
