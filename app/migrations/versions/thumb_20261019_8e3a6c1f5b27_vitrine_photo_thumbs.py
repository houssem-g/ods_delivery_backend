"""Vitrine photo thumbnails (QA 19/10/2026 M15: the menu downloaded the original 992×992 PNGs, 38 to
108 MB per visit): vitrine_products.photo_thumb_key, the key of the dish's 256 px WebP thumbnail in our
bucket (public/vitrine-thumbs/…), made on the first request (app/services/vitrine_thumbs.py).

Additive, nullable, nothing to backfill (made on demand); downgrade drops the column.

Revision ID: 8e3a6c1f5b27
Revises: b5d2f8a14c63
Create Date: 2026-10-19 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8e3a6c1f5b27"
down_revision: str | None = "b5d2f8a14c63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("vitrine_products", sa.Column("photo_thumb_key", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("vitrine_products", "photo_thumb_key")
