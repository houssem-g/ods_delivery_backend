"""revoked_access_tokens: « Se déconnecter » ends the access token at once (QA 08/10).

Before, the access token stayed valid until its expiry (up to 1 h) after a sign-out.

Revision ID: b8d4f0e2c3a5
Revises: a7c3e9d1b2f4
Create Date: 2026-10-12 23:45:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b8d4f0e2c3a5"
down_revision: str | None = "a7c3e9d1b2f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "revoked_access_tokens",
        sa.Column("jti", sa.Text(), primary_key=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_revoked_access_tokens_expires_at", "revoked_access_tokens", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_revoked_access_tokens_expires_at", table_name="revoked_access_tokens")
    op.drop_table("revoked_access_tokens")
