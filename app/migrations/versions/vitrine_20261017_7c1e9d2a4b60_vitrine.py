"""vitrine (étape 8, 16–17/10/2026): a shop with its own catalog, created by an admin only

- shops.visibility ('public' for every existing row | 'qa') and shops.slug (/v/<slug>);
- vitrines (1-1 shops): status, catalog source (manual | ods partner), hours, merchant link;
- vitrine_categories / vitrine_products (archived, never deleted), vitrine_posters (QR codes);
- partner_catalogs: the last snapshot each ODS restaurant pushed (PUT /api/partner/v1/catalog);
- vitrine_stats_daily + vitrine_visits (one view / scan per device and day, purged at 40 days);
- orders.vitrine_shop_id / cart / vitrine_src, orders.source accepts 'vitrine';
- order_stock_checks.ods_menu_ids (dishes the restaurant reported missing).
Additive only: no existing row changes meaning (no Vitrine exists, public_allowed defaults to false).
Contract: ods-audit/livraison-1610/vitrine/CONTRAT-A.md.

Revision ID: 7c1e9d2a4b60
Revises: ab1cb4ca5ec5
Create Date: 2026-10-17 00:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "7c1e9d2a4b60"
down_revision: str | None = "ab1cb4ca5ec5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "partner_catalogs",
        sa.Column("partner_id", sa.UUID(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("catalog_version", sa.Text(), nullable=True),
        sa.Column("test", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("products_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["partner_id"],
            ["partners.id"],
            name=op.f("fk_partner_catalogs_partner_id_partners"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("partner_id", name=op.f("pk_partner_catalogs")),
    )
    op.create_table(
        "vitrines",
        sa.Column("shop_id", sa.UUID(), nullable=False),
        sa.Column("status", sa.Text(), server_default="draft", nullable=False),
        sa.Column("source", sa.Text(), server_default="manual", nullable=False),
        sa.Column("partner_id", sa.UUID(), nullable=True),
        sa.Column("catalog_version", sa.Text(), nullable=True),
        sa.Column("catalog_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("catalog_test", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("hours", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True),
        sa.Column("merchant_token_hash", sa.Text(), nullable=True),
        sa.Column("merchant_token_enc", sa.Text(), nullable=True),
        sa.Column("merchant_link_no", sa.Integer(), server_default="0", nullable=False),
        sa.Column("merchant_link_opens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("merchant_link_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("manual_archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "source <> 'ods' OR partner_id IS NOT NULL", name=op.f("ck_vitrines_ods_has_partner")
        ),
        sa.CheckConstraint("source IN ('manual','ods')", name=op.f("ck_vitrines_source")),
        sa.CheckConstraint(
            "status IN ('draft','active','paused','archived')", name=op.f("ck_vitrines_status")
        ),
        sa.CheckConstraint(
            "merchant_link_no >= 0 AND merchant_link_opens >= 0", name=op.f("ck_vitrines_merchant_link")
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_vitrines_created_by_users"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["partner_id"], ["partners.id"], name=op.f("fk_vitrines_partner_id_partners"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["shop_id"], ["shops.id"], name=op.f("fk_vitrines_shop_id_shops"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("shop_id", name=op.f("pk_vitrines")),
        sa.UniqueConstraint("merchant_token_hash", name=op.f("uq_vitrines_merchant_token_hash")),
        sa.UniqueConstraint("partner_id", name=op.f("uq_vitrines_partner_id")),
    )
    op.create_index("ix_vitrines_status", "vitrines", ["status"], unique=False)
    op.create_table(
        "vitrine_categories",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("shop_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), server_default="0", nullable=False),
        sa.Column("ods_category_id", sa.BigInteger(), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("length(name) BETWEEN 1 AND 60", name=op.f("ck_vitrine_categories_name")),
        sa.ForeignKeyConstraint(
            ["shop_id"],
            ["vitrines.shop_id"],
            name=op.f("fk_vitrine_categories_shop_id_vitrines"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vitrine_categories")),
    )
    op.create_index("ix_vitrine_categories_shop_id", "vitrine_categories", ["shop_id"], unique=False)
    op.create_index(
        "vitrine_categories_ods_id",
        "vitrine_categories",
        ["shop_id", "ods_category_id"],
        unique=True,
        postgresql_where=sa.text("ods_category_id IS NOT NULL"),
    )
    op.create_table(
        "vitrine_posters",
        sa.Column("shop_id", sa.UUID(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("code ~ '^[a-z0-9-]{1,32}$'", name=op.f("ck_vitrine_posters_code")),
        sa.CheckConstraint("length(label) BETWEEN 1 AND 40", name=op.f("ck_vitrine_posters_label")),
        sa.ForeignKeyConstraint(
            ["shop_id"],
            ["vitrines.shop_id"],
            name=op.f("fk_vitrine_posters_shop_id_vitrines"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("shop_id", "code", name=op.f("pk_vitrine_posters")),
    )
    op.create_table(
        "vitrine_stats_daily",
        sa.Column("shop_id", sa.UUID(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("views", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "scans",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint("views >= 0", name=op.f("ck_vitrine_stats_daily_views")),
        sa.ForeignKeyConstraint(
            ["shop_id"],
            ["vitrines.shop_id"],
            name=op.f("fk_vitrine_stats_daily_shop_id_vitrines"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("shop_id", "day", name=op.f("pk_vitrine_stats_daily")),
    )
    op.create_table(
        "vitrine_visits",
        sa.Column("shop_id", sa.UUID(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("visitor", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["shop_id"],
            ["vitrines.shop_id"],
            name=op.f("fk_vitrine_visits_shop_id_vitrines"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("shop_id", "day", "kind", "visitor", name=op.f("pk_vitrine_visits")),
    )
    op.create_index("ix_vitrine_visits_day", "vitrine_visits", ["day"], unique=False)
    op.create_table(
        "vitrine_products",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("shop_id", sa.UUID(), nullable=False),
        sa.Column("category_id", sa.UUID(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("price", sa.Numeric(precision=10, scale=3), nullable=False),
        sa.Column("photo_key", sa.Text(), nullable=True),
        sa.Column("available", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("position", sa.Integer(), server_default="0", nullable=False),
        sa.Column("ods_menu_id", sa.BigInteger(), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("length(description) <= 300", name=op.f("ck_vitrine_products_description")),
        sa.CheckConstraint("length(name) BETWEEN 1 AND 100", name=op.f("ck_vitrine_products_name")),
        sa.CheckConstraint("price >= 0 AND price <= 2000", name=op.f("ck_vitrine_products_price")),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["vitrine_categories.id"],
            name=op.f("fk_vitrine_products_category_id_vitrine_categories"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["shop_id"],
            ["vitrines.shop_id"],
            name=op.f("fk_vitrine_products_shop_id_vitrines"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vitrine_products")),
    )
    op.create_index("ix_vitrine_products_shop_id", "vitrine_products", ["shop_id"], unique=False)
    op.create_index(
        "vitrine_products_ods_id",
        "vitrine_products",
        ["shop_id", "ods_menu_id"],
        unique=True,
        postgresql_where=sa.text("ods_menu_id IS NOT NULL"),
    )
    op.add_column(
        "order_stock_checks",
        sa.Column("ods_menu_ids", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True),
    )
    op.add_column("orders", sa.Column("vitrine_shop_id", sa.UUID(), nullable=True))
    op.add_column(
        "orders", sa.Column("cart", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True)
    )
    op.add_column("orders", sa.Column("vitrine_src", sa.Text(), nullable=True))
    op.create_index(
        "orders_vitrine_shop",
        "orders",
        ["vitrine_shop_id", "created_at"],
        unique=False,
        postgresql_where=sa.text("vitrine_shop_id IS NOT NULL"),
    )
    op.create_foreign_key(
        op.f("fk_orders_vitrine_shop_id_shops"),
        "orders",
        "shops",
        ["vitrine_shop_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.add_column("shops", sa.Column("visibility", sa.Text(), server_default="public", nullable=False))
    op.add_column("shops", sa.Column("slug", sa.Text(), nullable=True))
    op.create_unique_constraint(op.f("uq_shops_slug"), "shops", ["slug"])
    op.create_check_constraint(op.f("ck_shops_visibility"), "shops", "visibility IN ('public','qa')")
    op.create_check_constraint(op.f("ck_shops_slug"), "shops", "slug ~ '^[a-z0-9-]{3,80}$'")
    op.drop_constraint(op.f("ck_orders_source"), "orders", type_="check")
    op.create_check_constraint(op.f("ck_orders_source"), "orders", "source IN ('app','partner','vitrine')")
    op.create_check_constraint(
        op.f("ck_orders_vitrine_shop"), "orders", "(source = 'vitrine') = (vitrine_shop_id IS NOT NULL)"
    )
    op.create_check_constraint(
        op.f("ck_orders_cart"), "orders", "cart IS NULL OR jsonb_typeof(cart) = 'object'"
    )
    for table in ("vitrines", "vitrine_categories", "vitrine_products"):
        op.execute(
            f"CREATE TRIGGER trg_{table}_updated_at BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
        )


def downgrade() -> None:
    # Vitrine orders would break the old CHECK: they go back to plain app orders.
    op.drop_constraint(op.f("ck_orders_cart"), "orders", type_="check")
    op.drop_constraint(op.f("ck_orders_vitrine_shop"), "orders", type_="check")
    op.execute("UPDATE orders SET source = 'app' WHERE source = 'vitrine'")
    op.drop_constraint(op.f("ck_orders_source"), "orders", type_="check")
    op.create_check_constraint(op.f("ck_orders_source"), "orders", "source IN ('app','partner')")
    op.drop_constraint(op.f("ck_shops_slug"), "shops", type_="check")
    op.drop_constraint(op.f("ck_shops_visibility"), "shops", type_="check")
    op.drop_constraint(op.f("uq_shops_slug"), "shops", type_="unique")
    op.drop_column("shops", "slug")
    op.drop_column("shops", "visibility")
    op.drop_constraint(op.f("fk_orders_vitrine_shop_id_shops"), "orders", type_="foreignkey")
    op.drop_index(
        "orders_vitrine_shop", table_name="orders", postgresql_where=sa.text("vitrine_shop_id IS NOT NULL")
    )
    op.drop_column("orders", "vitrine_src")
    op.drop_column("orders", "cart")
    op.drop_column("orders", "vitrine_shop_id")
    op.drop_column("order_stock_checks", "ods_menu_ids")
    op.drop_index(
        "vitrine_products_ods_id",
        table_name="vitrine_products",
        postgresql_where=sa.text("ods_menu_id IS NOT NULL"),
    )
    op.drop_index("ix_vitrine_products_shop_id", table_name="vitrine_products")
    op.drop_table("vitrine_products")
    op.drop_index("ix_vitrine_visits_day", table_name="vitrine_visits")
    op.drop_table("vitrine_visits")
    op.drop_table("vitrine_stats_daily")
    op.drop_table("vitrine_posters")
    op.drop_index(
        "vitrine_categories_ods_id",
        table_name="vitrine_categories",
        postgresql_where=sa.text("ods_category_id IS NOT NULL"),
    )
    op.drop_index("ix_vitrine_categories_shop_id", table_name="vitrine_categories")
    op.drop_table("vitrine_categories")
    op.drop_index("ix_vitrines_status", table_name="vitrines")
    op.drop_table("vitrines")
    op.drop_table("partner_catalogs")
