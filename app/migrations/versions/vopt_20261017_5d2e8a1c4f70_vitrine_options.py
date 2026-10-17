"""vitrine options (17/10/2026): « ce que j'enlève » — the removals a customer asks on a dish

- vitrine_products.ods_ingredients: the dish's recipe as ODS pushed it ([{name, removable}], NULL =
  no recipe); vitrine_products.options_override: the admin's corrections (NULL = automatic).
- vitrine_option_stats: per dish and removal, how many ordered lines asked it (aggregated counts, no
  customer): only orders the chips.
The removals of an order live in its `cart` snapshot (orders.cart.lines[].mods): no column.
Additive only; downgrade drops the table and the two columns.

Revision ID: 5d2e8a1c4f70
Revises: 3f8b2c71d9a4
Create Date: 2026-10-17 15:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "5d2e8a1c4f70"
down_revision: str | None = "3f8b2c71d9a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "vitrine_products",
        sa.Column("ods_ingredients", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "vitrine_products",
        sa.Column("options_override", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_table(
        "vitrine_option_stats",
        sa.Column("shop_id", sa.UUID(), nullable=False),
        sa.Column("product_id", sa.UUID(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("count", sa.Integer(), server_default="0", nullable=False),
        sa.CheckConstraint("count >= 0", name=op.f("ck_vitrine_option_stats_count")),
        sa.CheckConstraint("length(key) BETWEEN 1 AND 48", name=op.f("ck_vitrine_option_stats_key")),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["vitrine_products.id"],
            name=op.f("fk_vitrine_option_stats_product_id_vitrine_products"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["shop_id"],
            ["vitrines.shop_id"],
            name=op.f("fk_vitrine_option_stats_shop_id_vitrines"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("product_id", "key", name=op.f("pk_vitrine_option_stats")),
    )
    op.create_index("ix_vitrine_option_stats_shop_id", "vitrine_option_stats", ["shop_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_vitrine_option_stats_shop_id", table_name="vitrine_option_stats")
    op.drop_table("vitrine_option_stats")
    op.drop_column("vitrine_products", "options_override")
    op.drop_column("vitrine_products", "ods_ingredients")
