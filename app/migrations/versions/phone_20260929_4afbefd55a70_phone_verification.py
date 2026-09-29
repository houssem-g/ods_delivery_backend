"""phone verification: users.phone_verified_at, phone_verifications, reset trigger

Owner's decision (2026-09-29): Tunisian numbers (+216) are accepted as they are; a customer
with a foreign number confirms it with a 6-digit code sent by WhatsApp before ordering.

- users.phone_verified_at: when phone_e164 was confirmed;
- phone_verifications: the codes (hash, 10 min, 5 attempts), also the rate-limit ledger;
- trigger trg_users_phone_unverify: any write that changes phone_e164 without setting
  phone_verified_at clears it (profile edit, admin, import...), so "verified" always means
  "this very number was confirmed".

Revision ID: 4afbefd55a70
Revises: 5c1d7e2a9b40
Create Date: 2026-09-29 10:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "4afbefd55a70"
down_revision: str | None = "5c1d7e2a9b40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UNVERIFY_FN = """
CREATE FUNCTION users_phone_unverify() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.phone_e164 IS DISTINCT FROM OLD.phone_e164
       AND NEW.phone_verified_at IS NOT DISTINCT FROM OLD.phone_verified_at THEN
        NEW.phone_verified_at := NULL;
    END IF;
    RETURN NEW;
END $$
"""


def upgrade() -> None:
    op.add_column("users", sa.Column("phone_verified_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "phone_verifications",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("phone_e164", sa.Text(), nullable=False),
        sa.Column("code_hash", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "phone_e164 ~ '^\\+[1-9][0-9]{7,14}$'", name=op.f("ck_phone_verifications_phone_e164")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_phone_verifications_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_phone_verifications")),
    )
    op.create_index(
        "ix_phone_verifications_open",
        "phone_verifications",
        ["user_id"],
        unique=False,
        postgresql_where=sa.text("used_at IS NULL"),
    )
    op.create_index(
        "ix_phone_verifications_phone_created_at",
        "phone_verifications",
        ["phone_e164", "created_at"],
        unique=False,
    )
    op.execute(UNVERIFY_FN)
    op.execute(
        "CREATE TRIGGER trg_users_phone_unverify BEFORE UPDATE OF phone_e164 ON users "
        "FOR EACH ROW EXECUTE FUNCTION users_phone_unverify()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_users_phone_unverify ON users")
    op.execute("DROP FUNCTION users_phone_unverify()")
    op.drop_index("ix_phone_verifications_phone_created_at", table_name="phone_verifications")
    op.drop_index(
        "ix_phone_verifications_open",
        table_name="phone_verifications",
        postgresql_where=sa.text("used_at IS NULL"),
    )
    op.drop_table("phone_verifications")
    op.drop_column("users", "phone_verified_at")
