"""order punctuality (18/10/2026): how late each delivered course was against the announced time

- orders.punctuality_late_minutes (−1440…1440, null = unknown): delivered_at − (accepted_at + eta_minutes),
  whole minutes, negative = early; written when the order is delivered (app/services/order_transitions.py).
  Shown to the courier himself only (getMyPunctuality), never to customers nor in the ranking.
- Backfill: the delivered orders that already have an acceptance time and a delay.

Additive; downgrade drops the column (and its CHECK).

Revision ID: 8e3a5c1f7b24
Revises: 2b7d4f9e6c13
Create Date: 2026-10-18 19:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8e3a5c1f7b24"
down_revision: str | None = "2b7d4f9e6c13"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("punctuality_late_minutes", sa.Integer(), nullable=True))
    op.create_check_constraint(
        op.f("ck_orders_punctuality_late_minutes"),
        "orders",
        "punctuality_late_minutes BETWEEN -1440 AND 1440",
    )
    op.execute(
        """
        UPDATE orders
           SET punctuality_late_minutes = GREATEST(-1440, LEAST(1440,
               round(extract(epoch FROM delivered_at - accepted_at) / 60 - eta_minutes)::int))
         WHERE status = 'delivered' AND accepted_at IS NOT NULL AND delivered_at IS NOT NULL
           AND eta_minutes IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_orders_punctuality_late_minutes"), "orders", type_="check")
    op.drop_column("orders", "punctuality_late_minutes")
