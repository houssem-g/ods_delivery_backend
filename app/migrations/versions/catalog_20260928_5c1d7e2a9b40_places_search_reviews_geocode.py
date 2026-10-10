"""catalog: places.search_norm, shop_reviews.target_key, geocode_cache, nullable courier phone

- places.search_norm (+ trigram index): normalized name/address/city, so the
  text scores of searchPlaces / searchByBbox / geocodeAddress run in SQL
  instead of scoring up to 4 000 rows in memory;
- shop_reviews.target_key: the legacy `shop_osm_id` as the front sends it. Some keys
  ('place:<name>@<lat>,<lng>') can't always be resolved to a shop or a place, so the
  one-target check becomes "at most one"; one review per user and key;
- geocode_cache: Nominatim answers, as its usage policy asks;
- couriers.phone_e164 nullable: deleteMyAccount anonymizes the courier row, which the
  delivered orders keep referencing.

Revision ID: 5c1d7e2a9b40
Revises: a466ae84fddb
Create Date: 2026-09-28 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "5c1d7e2a9b40"
down_revision: str | None = "a466ae84fddb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DELETED_PHONE_PLACEHOLDER = "+10000000000"


def upgrade() -> None:
    op.add_column("places", sa.Column("search_norm", sa.Text(), server_default="", nullable=False))
    op.create_index(
        "places_search",
        "places",
        ["search_norm"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"search_norm": "gin_trgm_ops"},
    )

    op.add_column("shop_reviews", sa.Column("target_key", sa.Text(), nullable=True))
    op.execute(
        """
        UPDATE shop_reviews r
        SET target_key = coalesce(
            (SELECT 'shop:' || r.shop_id::text WHERE r.shop_id IS NOT NULL),
            (SELECT p.osm_id FROM places p WHERE p.id = r.place_id),
            'review:' || r.id::text)
        """
    )
    op.alter_column("shop_reviews", "target_key", nullable=False)
    op.drop_constraint(op.f("ck_shop_reviews_one_target"), "shop_reviews", type_="check")
    op.create_check_constraint(
        op.f("ck_shop_reviews_one_target"), "shop_reviews", "num_nonnulls(shop_id, place_id) <= 1"
    )
    op.create_index("ix_shop_reviews_target_key", "shop_reviews", ["target_key"], unique=False)
    op.create_index("one_review_per_user_target", "shop_reviews", ["user_id", "target_key"], unique=True)

    op.create_table(
        "geocode_cache",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("found", sa.Boolean(), nullable=False),
        sa.Column("lat", sa.Double(), nullable=True),
        sa.Column("lng", sa.Double(), nullable=True),
        sa.Column(
            "result",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_geocode_cache")),
    )

    op.alter_column("couriers", "phone_e164", existing_type=sa.Text(), nullable=True)


def downgrade() -> None:
    op.execute(f"UPDATE couriers SET phone_e164 = '{DELETED_PHONE_PLACEHOLDER}' WHERE phone_e164 IS NULL")
    op.alter_column("couriers", "phone_e164", existing_type=sa.Text(), nullable=False)

    op.drop_table("geocode_cache")

    op.drop_index("one_review_per_user_target", table_name="shop_reviews")
    op.drop_index("ix_shop_reviews_target_key", table_name="shop_reviews")
    op.execute("DELETE FROM shop_reviews WHERE shop_id IS NULL AND place_id IS NULL")
    op.drop_constraint(op.f("ck_shop_reviews_one_target"), "shop_reviews", type_="check")
    op.create_check_constraint(
        op.f("ck_shop_reviews_one_target"), "shop_reviews", "(shop_id IS NULL) <> (place_id IS NULL)"
    )
    op.drop_column("shop_reviews", "target_key")

    op.drop_index("places_search", table_name="places", postgresql_using="gin")
    op.drop_column("places", "search_norm")
