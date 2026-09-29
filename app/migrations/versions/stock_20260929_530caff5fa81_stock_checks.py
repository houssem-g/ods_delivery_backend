"""stock checks: an item is missing at the shop, the customer decides (owner's request, 2026-09-29)

- orders.unavailable_policy: what applies when the customer does not answer in time
  (call_me | substitute | skip | cancel, default call_me), chosen in NewOrder;
- order_stock_checks: one row per "article indisponible" report of the courier (at most one
  pending per order), its deadline and the decision (customer or system);
- notification types stock_check (to the customer) and stock_check_answered (to the courier).

Revision ID: 530caff5fa81
Revises: b7e1c2d3a4f5
Create Date: 2026-09-29 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "530caff5fa81"
down_revision: str | None = "b7e1c2d3a4f5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_TYPES = (
    "order_accepted", "at_shop", "purchased", "on_the_way", "delivered", "order_cancelled",
    "delivery_cancelled", "delivery_delayed", "eta_update", "new_order", "new_offer", "new_message",
    "emergency_contact", "customer_responded", "customer_no_response_final", "order_confirmed",
    "order_preparing", "hot_deal_reserved", "issue_reported", "account_verified", "account_rejected",
)  # fmt: skip
NEW_TYPES = (*OLD_TYPES, "stock_check", "stock_check_answered")


def _in(values: Sequence[str]) -> str:
    return ",".join(f"'{v}'" for v in values)


def upgrade() -> None:
    op.add_column(
        "orders", sa.Column("unavailable_policy", sa.Text(), server_default="call_me", nullable=False)
    )
    op.create_check_constraint(
        op.f("ck_orders_unavailable_policy"),
        "orders",
        "unavailable_policy IN ('call_me','substitute','skip','cancel')",
    )
    op.create_table(
        "order_stock_checks",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("order_id", sa.UUID(), nullable=False),
        sa.Column("courier_id", sa.UUID(), nullable=True),
        sa.Column("missing_text", sa.Text(), nullable=False),
        sa.Column("substitute_text", sa.Text(), nullable=True),
        sa.Column("substitute_price", sa.Numeric(precision=10, scale=3), nullable=True),
        sa.Column("photo_key", sa.Text(), nullable=True),
        sa.Column("nothing_available", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("decided_by", sa.Text(), nullable=True),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending','substitute_accepted','item_skipped','order_cancelled','expired')",
            name=op.f("ck_order_stock_checks_status"),
        ),
        sa.CheckConstraint(
            "decided_by IN ('customer','system')", name=op.f("ck_order_stock_checks_decided_by")
        ),
        sa.CheckConstraint(
            "length(missing_text) BETWEEN 1 AND 500", name=op.f("ck_order_stock_checks_missing_text")
        ),
        sa.CheckConstraint(
            "length(substitute_text) <= 500", name=op.f("ck_order_stock_checks_substitute_text")
        ),
        sa.CheckConstraint(
            "substitute_price >= 0 AND substitute_price <= 2000",
            name=op.f("ck_order_stock_checks_substitute_price"),
        ),
        sa.ForeignKeyConstraint(
            ["courier_id"],
            ["couriers.id"],
            name=op.f("fk_order_stock_checks_courier_id_couriers"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"], name=op.f("fk_order_stock_checks_order_id_orders"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_order_stock_checks")),
    )
    op.create_index(
        "one_pending_stock_check_per_order",
        "order_stock_checks",
        ["order_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "due_stock_checks",
        "order_stock_checks",
        ["deadline_at"],
        unique=False,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_order_stock_checks_order_id_created_at",
        "order_stock_checks",
        ["order_id", "created_at"],
        unique=False,
    )
    op.drop_constraint(op.f("ck_notifications_type"), "notifications", type_="check")
    op.create_check_constraint(op.f("ck_notifications_type"), "notifications", f"type IN ({_in(NEW_TYPES)})")


def downgrade() -> None:
    op.execute(f"DELETE FROM notifications WHERE type NOT IN ({_in(OLD_TYPES)})")
    op.drop_constraint(op.f("ck_notifications_type"), "notifications", type_="check")
    op.create_check_constraint(op.f("ck_notifications_type"), "notifications", f"type IN ({_in(OLD_TYPES)})")
    op.drop_index("ix_order_stock_checks_order_id_created_at", table_name="order_stock_checks")
    op.drop_index("due_stock_checks", table_name="order_stock_checks", postgresql_where=sa.text("status = 'pending'"))
    op.drop_index(
        "one_pending_stock_check_per_order",
        table_name="order_stock_checks",
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.drop_table("order_stock_checks")
    op.drop_constraint(op.f("ck_orders_unavailable_policy"), "orders", type_="check")
    op.drop_column("orders", "unavailable_policy")
