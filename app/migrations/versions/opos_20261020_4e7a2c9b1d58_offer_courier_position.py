"""offer courier position (owner, 20/10/2026): where the courier was when he sent / last changed an offer

- order_offers.courier_lat / courier_lng (Numeric(7, 4), ~11 m, inside ORDER_BOUNDS) and
  order_offers.courier_position_at: the position the arrival was computed from (the app's from_lat /
  from_lng, else his recent server position — app/services/arrival.py courier_fix). Null: unknown
  (older offers, no GPS). Shown on the admin's « Voir le trajet » map only (getAdminOrderRoute).

Additive only: the installed apps read and write the same fields as before. Downgrade drops the three
columns (and their CHECK); nothing else depends on them.

Revision ID: 4e7a2c9b1d58
Revises: 8d3b5f1e2a64
Create Date: 2026-10-20 16:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "4e7a2c9b1d58"
down_revision: str | None = "8d3b5f1e2a64"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("order_offers", sa.Column("courier_lat", sa.Numeric(7, 4), nullable=True))
    op.add_column("order_offers", sa.Column("courier_lng", sa.Numeric(7, 4), nullable=True))
    op.add_column("order_offers", sa.Column("courier_position_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        op.f("ck_order_offers_courier_position"),
        "order_offers",
        "(courier_lat IS NULL AND courier_lng IS NULL AND courier_position_at IS NULL)"
        " OR (courier_lat BETWEEN 29.5 AND 38.5 AND courier_lng BETWEEN 7.0 AND 12.5"
        " AND courier_position_at IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_order_offers_courier_position"), "order_offers", type_="check")
    op.drop_column("order_offers", "courier_position_at")
    op.drop_column("order_offers", "courier_lng")
    op.drop_column("order_offers", "courier_lat")
