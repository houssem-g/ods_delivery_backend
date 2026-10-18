"""offer arrival (18/10/2026): the server's arrival estimate stored on each courier offer

- order_offers.estimated_eta_minutes (0-600, null = unknown: no courier position, older offers): courier →
  shop + the shop's average wait + shop → customer, computed when the offer is sent or changed
  (app/services/arrival.py).
- order_offers.to_shop_km (≥ 0): how far the courier was from the shop then (the customer's card).
- order_offers.eta_announced (default false): his delay is clearly under the computation, the customer
  reads « annoncé ».

Additive only: the installed apps read and write the same fields as before. Downgrade drops the three
columns (and their CHECKs); nothing else depends on them.

Revision ID: 2b7d4f9e6c13
Revises: 6a1d4e8b2f37
Create Date: 2026-10-18 15:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "2b7d4f9e6c13"
down_revision: str | None = "6a1d4e8b2f37"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("order_offers", sa.Column("estimated_eta_minutes", sa.Integer(), nullable=True))
    op.add_column("order_offers", sa.Column("to_shop_km", sa.Numeric(6, 2), nullable=True))
    op.add_column(
        "order_offers",
        sa.Column("eta_announced", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.create_check_constraint(
        op.f("ck_order_offers_estimated_eta_minutes"),
        "order_offers",
        "estimated_eta_minutes BETWEEN 0 AND 600",
    )
    op.create_check_constraint(
        op.f("ck_order_offers_to_shop_km"), "order_offers", "to_shop_km >= 0 AND to_shop_km <= 1000"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_order_offers_to_shop_km"), "order_offers", type_="check")
    op.drop_constraint(op.f("ck_order_offers_estimated_eta_minutes"), "order_offers", type_="check")
    op.drop_column("order_offers", "eta_announced")
    op.drop_column("order_offers", "to_shop_km")
    op.drop_column("order_offers", "estimated_eta_minutes")
