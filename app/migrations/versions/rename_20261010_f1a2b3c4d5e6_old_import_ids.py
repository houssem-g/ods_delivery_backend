"""old import ids: the columns holding the id a row had on the previous platform are named
old_import_id / old_import_profile_id (owner, 10/10/2026: no trace of that platform's name).

Databases created before this revision get their columns and unique constraints renamed;
newer ones are created with the new names already (initial schema), so each rename is skipped
when the old column is absent.

Revision ID: f1a2b3c4d5e6
Revises: e9f4a6b8c0d3
Create Date: 2026-10-10 23:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f1a2b3c4d5e6"
down_revision: str | None = "e9f4a6b8c0d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (
    "places", "users", "couriers", "device_tokens", "shops", "orders", "shop_reviews", "hot_deals",
    "messages", "no_response_cases", "notifications", "order_offers", "outbound_messages",
)  # fmt: skip


def _has_column(table: str, column: str) -> bool:
    return bool(
        op.get_bind().execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() "
                "AND table_name = :t AND column_name = :c"
            ),
            {"t": table, "c": column},
        ).first()
    )


def _rename(table: str, old: str, new: str) -> None:
    if not _has_column(table, old):
        return
    op.alter_column(table, old, new_column_name=new)
    op.execute(f'ALTER TABLE "{table}" RENAME CONSTRAINT "uq_{table}_{old}" TO "uq_{table}_{new}"')


def upgrade() -> None:
    for table in TABLES:
        _rename(table, "legacy_b44_id", "old_import_id")
    _rename("users", "legacy_profile_b44_id", "old_import_profile_id")


def downgrade() -> None:
    for table in TABLES:
        _rename(table, "old_import_id", "legacy_b44_id")
    _rename("users", "old_import_profile_id", "legacy_profile_b44_id")
