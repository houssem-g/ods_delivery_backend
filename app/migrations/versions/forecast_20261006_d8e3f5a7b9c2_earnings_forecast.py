"""courier earnings forecast: self-declared activity, client estimates, snapshots, learned factors

- couriers.declared_weekly_deliveries / declared_active_days / declared_regular_clients / declared_at;
- users.declared_monthly_orders (the customer's « combien de fois par mois ? »);
- courier_client_estimates: the courier's estimate for each of his invited clients;
- earnings_forecasts: week snapshots (compared with reality once over) and month snapshots;
- forecast_factors: what the forecast learned across couriers (weekday, holiday, Ramadan, rain, bias).

Revision ID: d8e3f5a7b9c2
Revises: c7d2e4f6a8b1
Create Date: 2026-10-06 21:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d8e3f5a7b9c2"
down_revision: str | None = "c7d2e4f6a8b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("couriers", sa.Column("declared_weekly_deliveries", sa.Integer(), nullable=True))
    op.add_column("couriers", sa.Column("declared_active_days", sa.Integer(), nullable=True))
    op.add_column("couriers", sa.Column("declared_regular_clients", sa.Integer(), nullable=True))
    op.add_column("couriers", sa.Column("declared_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        op.f("ck_couriers_declared_activity"),
        "couriers",
        "(declared_weekly_deliveries IS NULL OR declared_weekly_deliveries BETWEEN 0 AND 300)"
        " AND (declared_active_days IS NULL OR declared_active_days BETWEEN 0 AND 7)"
        " AND (declared_regular_clients IS NULL OR declared_regular_clients BETWEEN 0 AND 500)",
    )
    op.add_column("users", sa.Column("declared_monthly_orders", sa.Integer(), nullable=True))
    op.create_check_constraint(
        op.f("ck_users_declared_monthly_orders"),
        "users",
        "declared_monthly_orders IS NULL OR declared_monthly_orders BETWEEN 0 AND 60",
    )

    op.create_table(
        "courier_client_estimates",
        sa.Column("courier_id", sa.UUID(), nullable=False),
        sa.Column("customer_id", sa.UUID(), nullable=False),
        sa.Column("monthly_orders", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("monthly_orders BETWEEN 0 AND 60", name=op.f("ck_courier_client_estimates_monthly_orders")),
        sa.ForeignKeyConstraint(
            ["courier_id"], ["couriers.id"], name=op.f("fk_courier_client_estimates_courier_id_couriers"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"], ["users.id"], name=op.f("fk_courier_client_estimates_customer_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("courier_id", "customer_id", name=op.f("pk_courier_client_estimates")),
    )  # fmt: skip

    op.create_table(
        "earnings_forecasts",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("courier_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("p25", sa.Numeric(10, 3), nullable=False),
        sa.Column("p50", sa.Numeric(10, 3), nullable=False),
        sa.Column("p75", sa.Numeric(10, 3), nullable=False),
        sa.Column("expected_deliveries", sa.Numeric(10, 3), nullable=False),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"),
                  nullable=False),
        sa.Column("actual_fees", sa.Numeric(10, 3), nullable=True),
        sa.Column("actual_deliveries", sa.Integer(), nullable=True),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("kind IN ('week','month')", name=op.f("ck_earnings_forecasts_kind")),
        sa.ForeignKeyConstraint(
            ["courier_id"], ["couriers.id"], name=op.f("fk_earnings_forecasts_courier_id_couriers"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_earnings_forecasts")),
        sa.UniqueConstraint(
            "courier_id", "kind", "period_start", "as_of",
            name=op.f("uq_earnings_forecasts_courier_id_kind_period_start_as_of"),
        ),
    )  # fmt: skip
    op.create_index(
        "earnings_forecasts_to_evaluate", "earnings_forecasts", ["period_end"], unique=False,
        postgresql_where=sa.text("evaluated_at IS NULL"),
    )  # fmt: skip

    op.create_table(
        "forecast_factors",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", sa.Numeric(10, 4), nullable=False),
        sa.Column("weight", sa.Numeric(12, 3), server_default="0", nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_forecast_factors")),
    )


def downgrade() -> None:
    op.drop_table("forecast_factors")
    op.drop_index(
        "earnings_forecasts_to_evaluate", table_name="earnings_forecasts",
        postgresql_where=sa.text("evaluated_at IS NULL"),
    )  # fmt: skip
    op.drop_table("earnings_forecasts")
    op.drop_table("courier_client_estimates")
    op.drop_constraint(op.f("ck_users_declared_monthly_orders"), "users", type_="check")
    op.drop_column("users", "declared_monthly_orders")
    op.drop_constraint(op.f("ck_couriers_declared_activity"), "couriers", type_="check")
    op.drop_column("couriers", "declared_at")
    op.drop_column("couriers", "declared_regular_clients")
    op.drop_column("couriers", "declared_active_days")
    op.drop_column("couriers", "declared_weekly_deliveries")
