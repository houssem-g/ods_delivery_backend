"""device_tokens.signed_out_at: launch mode (owner, 13/10/2026).

A courier who signs out while the launch mode is on keeps hearing about new orders on his phone
(only those); the next sign-in clears it. See app/services/launch_mode.py.

Revision ID: c9e5a1f3d4b6
Revises: b8d4f0e2c3a5
Create Date: 2026-10-13 10:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9e5a1f3d4b6"
down_revision: str | None = "b8d4f0e2c3a5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("device_tokens", sa.Column("signed_out_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("device_tokens", "signed_out_at")
