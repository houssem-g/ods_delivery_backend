"""vitrine extras (17/10/2026): the paid extras of a dish linked to ODS

- vitrine_products.ods_extras: the dish's questions as ODS pushed them
  ([{id, name, multiple, choices: [{id, name, price}]}], NULL = none). The extras of an order live in its
  `cart` snapshot (orders.cart.lines[].extras): no other column.
Additive only; downgrade drops the column.

Revision ID: 9c4e2b7a1d35
Revises: 5d2e8a1c4f70
Create Date: 2026-10-17 19:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "9c4e2b7a1d35"
down_revision: str | None = "5d2e8a1c4f70"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "vitrine_products",
        sa.Column("ods_extras", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("vitrine_products", "ods_extras")
