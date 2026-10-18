"""invite-link head start (owner 18/10/2026): the customer's own courier alone sees his order 60 s

- orders.priority_courier_id (→ couriers, SET NULL): the invite-link courier who got the head start;
- orders.priority_until: when it ends (the other couriers see the order from then on);
- orders.priority_released_at: when the other couriers were told (NULL while it runs: the job
  `invite_priority_release` picks it up, also after a restart);
- partial index orders_priority_due on the head starts still to end.

Additive, nothing to backfill (no past order had a head start); downgrade drops the three columns.

Revision ID: 4c7b1e9a3d52
Revises: 8e3a5c1f7b24
Create Date: 2026-10-18 21:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "4c7b1e9a3d52"
down_revision: str | None = "8e3a5c1f7b24"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("priority_courier_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("orders", sa.Column("priority_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column("orders", sa.Column("priority_released_at", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key(
        op.f("fk_orders_priority_courier_id_couriers"),
        "orders",
        "couriers",
        ["priority_courier_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "orders_priority_due",
        "orders",
        ["priority_until"],
        postgresql_where=sa.text("priority_until IS NOT NULL AND priority_released_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("orders_priority_due", table_name="orders")
    op.drop_constraint(op.f("fk_orders_priority_courier_id_couriers"), "orders", type_="foreignkey")
    op.drop_column("orders", "priority_released_at")
    op.drop_column("orders", "priority_until")
    op.drop_column("orders", "priority_courier_id")
