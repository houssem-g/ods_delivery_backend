"""order drafts: unfinished NewOrder forms kept 24 h (owner's request, 2026-09-29)

"Les commandes non finies restent comme brouillon pendant 24h et je dois pouvoir les
supprimer." Server side, so a draft survives a reinstall or a change of device.
Expired rows are purged hourly (job `purge_order_drafts`).

Revision ID: 6691f4426902
Revises: 4afbefd55a70
Create Date: 2026-09-29 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "6691f4426902"
down_revision: str | None = "4afbefd55a70"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "order_drafts",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("title", sa.Text(), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(title) <= 120", name=op.f("ck_order_drafts_title")),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_order_drafts_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_order_drafts")),
    )
    op.create_index(
        "ix_order_drafts_user_id_expires_at", "order_drafts", ["user_id", "expires_at"], unique=False
    )
    op.create_index("ix_order_drafts_expires_at", "order_drafts", ["expires_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_order_drafts_expires_at", table_name="order_drafts")
    op.drop_index("ix_order_drafts_user_id_expires_at", table_name="order_drafts")
    op.drop_table("order_drafts")
