"""« Continuer avec Google » from the phone app (owner 19/10/2026): auth_handoffs, the 10-minute
single-use link between the phone's browser (Google sign-in) and the app waiting for its session.

Additive (one new table); downgrade drops it.

Revision ID: 8d3b5f1e2a64
Revises: 6c2f8e1a9d47
Create Date: 2026-10-19 20:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "8d3b5f1e2a64"
down_revision: str | None = "6c2f8e1a9d47"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "auth_handoffs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("secret_hash", sa.Text(), nullable=False),
        sa.Column("client_ip", sa.Text(), nullable=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_auth_handoffs_expires_at", "auth_handoffs", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_auth_handoffs_expires_at", table_name="auth_handoffs")
    op.drop_table("auth_handoffs")
