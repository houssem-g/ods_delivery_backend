"""places.search_norm: Latin ↔ Arabic sound skeletons (QA 08/10, B3).

« monoprix » must find « مونوبري » and « كارفور » must find « Carrefour »: search_norm now ends
with the sound skeleton of every word (« ~mnbr »), see app/services/text_norm.py. Every existing
place is recomputed once (about 10 000 rows in production, a few seconds).

Revision ID: a7c3e9d1b2f4
Revises: f1a2b3c4d5e6
Create Date: 2026-10-12 23:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.services import text_norm

revision: str = "a7c3e9d1b2f4"
down_revision: str | None = "f1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BATCH = 1000


def _recompute(compute) -> None:
    bind = op.get_bind()
    last = None
    while True:
        rows = bind.execute(
            sa.text(
                "SELECT id, name, address, city FROM places "
                + ("WHERE id > :last " if last is not None else "")
                + "ORDER BY id LIMIT :n"
            ),
            {"last": last, "n": BATCH} if last is not None else {"n": BATCH},
        ).all()
        if not rows:
            return
        bind.execute(
            sa.text("UPDATE places SET search_norm = :s WHERE id = :id"),
            [{"id": r.id, "s": compute(r.name, r.address, r.city) or "-"} for r in rows],
        )
        last = rows[-1].id


def upgrade() -> None:
    _recompute(text_norm.search_text)


def downgrade() -> None:
    _recompute(text_norm.normalize_text)
