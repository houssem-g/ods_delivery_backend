"""onboarding drafts (owner 18/10/2026: the sign-up restarted from zero after reading the e-mailed
code or taking the ID photo): the unfinished courier onboarding / customer profile setup kept
server side, one row per (user, kind), 30 days after its last save.

- onboarding_drafts(user_id → users CASCADE, kind 'courier'|'customer', payload jsonb, created_at,
  updated_at, expires_at), primary key (user_id, kind), index on expires_at for the hourly purge.

Additive, nothing to backfill; downgrade drops the table.

Revision ID: b5d2f8a14c63
Revises: 4c7b1e9a3d52
Create Date: 2026-10-18 23:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b5d2f8a14c63"
down_revision: str | None = "4c7b1e9a3d52"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "onboarding_drafts",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('courier','customer')", name=op.f("ck_onboarding_drafts_kind")),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_onboarding_drafts_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("user_id", "kind", name=op.f("pk_onboarding_drafts")),
    )
    op.create_index("ix_onboarding_drafts_expires_at", "onboarding_drafts", ["expires_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_onboarding_drafts_expires_at", table_name="onboarding_drafts")
    op.drop_table("onboarding_drafts")
