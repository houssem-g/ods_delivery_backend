"""courier location_at (QA 20/10/2026 m4, d1-F1): when couriers.last_location was taken

couriers.last_seen_at was both the presence heartbeat (4 h → offline) and the age of the stored
position. A heartbeat without a position (the app open, GPS refused) must keep him online WITHOUT
making an old position look fresh (arrival « vérifié » from it, the customer's map « live »).
location_at = the moment of last_location; backfilled from last_seen_at where a position exists.

Additive only: the installed apps read and write the same fields as before.

Revision ID: b7c3e1d9a5f2
Revises: 4e7a2c9b1d58
Create Date: 2026-10-20 22:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7c3e1d9a5f2"
down_revision: str | None = "4e7a2c9b1d58"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("couriers", sa.Column("location_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE couriers SET location_at = last_seen_at WHERE last_location IS NOT NULL")


def downgrade() -> None:
    op.drop_column("couriers", "location_at")
