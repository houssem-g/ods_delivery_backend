"""device keys: « Se connecter avec l'empreinte / le visage » from the phone app

Revision ID: e9f4a6b8c0d3
Revises: d8e3f5a7b9c2
Create Date: 2026-10-08 21:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e9f4a6b8c0d3"
down_revision: str | None = "d8e3f5a7b9c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "device_keys",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("secret_hash", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_device_keys_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_device_keys")),
        sa.UniqueConstraint("secret_hash", name=op.f("uq_device_keys_secret_hash")),
    )
    op.create_index(op.f("ix_device_keys_user_id"), "device_keys", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_device_keys_user_id"), table_name="device_keys")
    op.drop_table("device_keys")
