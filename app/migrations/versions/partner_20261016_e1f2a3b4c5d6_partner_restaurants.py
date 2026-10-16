"""partner restaurants (Lot 2, 2A/2B — owner's decisions of 16/10/2026)

- partners: an ODS restaurant with its hashed API key, its technical Delivery user, its webhook,
  its open-order cap, the D5 counter « retours refusés » and the D4 debt;
- orders.source / partner_id / partner_ref (unique per partner: idempotence) / partner_request_hash /
  tracking_token (public tracking without an account) / partner_return_outcome;
- partner_customer_stats: the « n'a pas payé X fois sur ses Y dernières commandes » badge per phone
  number (sha256) over the partner orders;
- partner_webhook_deliveries: outgoing signed events, retried 1 → 16 min.

Revision ID: e1f2a3b4c5d6
Revises: c9e5a1f3d4b6
Create Date: 2026-10-16 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e1f2a3b4c5d6"
down_revision: str | None = "c9e5a1f3d4b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "partners",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), server_default="ods_restaurant", nullable=False),
        sa.Column("ods_restaurant_id", sa.Integer(), nullable=True),
        sa.Column("key_prefix", sa.Text(), nullable=False),
        sa.Column("key_hash", sa.Text(), nullable=False),
        sa.Column("technical_user_id", sa.UUID(), nullable=False),
        sa.Column("webhook_url", sa.Text(), nullable=True),
        sa.Column("webhook_secret", sa.Text(), nullable=True),
        sa.Column("max_open_orders", sa.Integer(), server_default="50", nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("returns_refused", sa.Integer(), server_default="0", nullable=False),
        sa.Column("debt_amount", sa.Numeric(10, 3), server_default="0", nullable=False),
        sa.Column("debt_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("kind IN ('ods_restaurant')", name=op.f("ck_partners_kind")),
        sa.CheckConstraint("length(name) BETWEEN 1 AND 120", name=op.f("ck_partners_name")),
        sa.CheckConstraint("max_open_orders BETWEEN 1 AND 500", name=op.f("ck_partners_max_open_orders")),
        sa.CheckConstraint("returns_refused >= 0", name=op.f("ck_partners_returns_refused")),
        sa.CheckConstraint("debt_amount >= 0", name=op.f("ck_partners_debt_amount")),
        sa.ForeignKeyConstraint(
            ["technical_user_id"],
            ["users.id"],
            name=op.f("fk_partners_technical_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_partners_created_by_users"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_partners")),
        sa.UniqueConstraint("key_hash", name=op.f("uq_partners_key_hash")),
        sa.UniqueConstraint("technical_user_id", name=op.f("uq_partners_technical_user_id")),
    )
    op.create_index("ix_partners_ods_restaurant_id", "partners", ["ods_restaurant_id"], unique=False)
    op.execute(
        "CREATE TRIGGER trg_partners_updated_at BEFORE UPDATE ON partners "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )

    op.create_table(
        "partner_customer_stats",
        sa.Column("phone_hash", sa.Text(), nullable=False),
        sa.Column("total_orders", sa.Integer(), server_default="0", nullable=False),
        sa.Column("delivered_orders", sa.Integer(), server_default="0", nullable=False),
        sa.Column("recent_orders", sa.Integer(), server_default="0", nullable=False),
        sa.Column("recent_unpaid", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_order_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("phone_hash", name=op.f("pk_partner_customer_stats")),
    )
    op.execute(
        "CREATE TRIGGER trg_partner_customer_stats_updated_at BEFORE UPDATE ON partner_customer_stats "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )

    op.add_column("orders", sa.Column("source", sa.Text(), server_default="app", nullable=False))
    op.add_column("orders", sa.Column("partner_id", sa.UUID(), nullable=True))
    op.add_column("orders", sa.Column("partner_ref", sa.Text(), nullable=True))
    op.add_column("orders", sa.Column("partner_request_hash", sa.Text(), nullable=True))
    op.add_column("orders", sa.Column("tracking_token", sa.Text(), nullable=True))
    op.add_column("orders", sa.Column("partner_return_outcome", sa.Text(), nullable=True))
    op.create_check_constraint(op.f("ck_orders_source"), "orders", "source IN ('app','partner')")
    op.create_check_constraint(
        op.f("ck_orders_partner_return_outcome"),
        "orders",
        "partner_return_outcome IN ('accepted','refused','shop_closed')",
    )
    op.create_check_constraint(
        op.f("ck_orders_partner_ref_with_partner"), "orders", "(partner_id IS NULL) = (partner_ref IS NULL)"
    )
    op.create_foreign_key(
        op.f("fk_orders_partner_id_partners"),
        "orders",
        "partners",
        ["partner_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_unique_constraint(op.f("uq_orders_tracking_token"), "orders", ["tracking_token"])
    op.create_index(op.f("ix_orders_partner_id"), "orders", ["partner_id"], unique=False)
    op.create_index(
        "orders_partner_ref",
        "orders",
        ["partner_id", "partner_ref"],
        unique=True,
        postgresql_where=sa.text("partner_id IS NOT NULL"),
    )
    op.create_index(
        "orders_partner_phone",
        "orders",
        ["contact_phone_e164", sa.text("created_at DESC")],
        unique=False,
        postgresql_where=sa.text("source = 'partner'"),
    )

    op.create_table(
        "partner_webhook_deliveries",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("partner_id", sa.UUID(), nullable=False),
        sa.Column("order_id", sa.UUID(), nullable=True),
        sa.Column("event", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "next_attempt_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending','delivered','failed')", name=op.f("ck_partner_webhook_deliveries_status")
        ),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_partner_webhook_deliveries_attempts")),
        sa.ForeignKeyConstraint(
            ["partner_id"],
            ["partners.id"],
            name=op.f("fk_partner_webhook_deliveries_partner_id_partners"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name=op.f("fk_partner_webhook_deliveries_order_id_orders"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_partner_webhook_deliveries")),
    )
    op.create_index(
        "partner_webhook_deliveries_due",
        "partner_webhook_deliveries",
        ["next_attempt_at"],
        unique=False,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_partner_webhook_deliveries_partner_id_created_at",
        "partner_webhook_deliveries",
        ["partner_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_partner_webhook_deliveries_order_id", "partner_webhook_deliveries", ["order_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_partner_webhook_deliveries_order_id", table_name="partner_webhook_deliveries")
    op.drop_index(
        "ix_partner_webhook_deliveries_partner_id_created_at", table_name="partner_webhook_deliveries"
    )
    op.drop_index("partner_webhook_deliveries_due", table_name="partner_webhook_deliveries")
    op.drop_table("partner_webhook_deliveries")

    op.drop_index("orders_partner_phone", table_name="orders")
    op.drop_index("orders_partner_ref", table_name="orders")
    op.drop_index(op.f("ix_orders_partner_id"), table_name="orders")
    op.drop_constraint(op.f("uq_orders_tracking_token"), "orders", type_="unique")
    op.drop_constraint(op.f("fk_orders_partner_id_partners"), "orders", type_="foreignkey")
    op.drop_constraint(op.f("ck_orders_partner_ref_with_partner"), "orders", type_="check")
    op.drop_constraint(op.f("ck_orders_partner_return_outcome"), "orders", type_="check")
    op.drop_constraint(op.f("ck_orders_source"), "orders", type_="check")
    for column in (
        "partner_return_outcome",
        "tracking_token",
        "partner_request_hash",
        "partner_ref",
        "partner_id",
        "source",
    ):
        op.drop_column("orders", column)

    op.execute("DROP TRIGGER IF EXISTS trg_partner_customer_stats_updated_at ON partner_customer_stats")
    op.drop_table("partner_customer_stats")
    op.execute("DROP TRIGGER IF EXISTS trg_partners_updated_at ON partners")
    op.drop_index("ix_partners_ods_restaurant_id", table_name="partners")
    op.drop_table("partners")
