"""recipe addresses without « halal » / « jambon » (owner 23/10, QA v3: the word showed in the address bar and
in the link of « Partager »)

The SAME row is renamed (same id: its photos, step videos, recipe orders and the favourites kept on the phones,
all by id, stay with it); the old address keeps working through recipes.RENAMED_SLUGS (getRecipe answers with the
new slug, the app replaces the address). data/recipes/*.json carry the new ids, so a re-import updates this row
(upsert by slug) instead of creating a twin. A rename is skipped when the new address is already taken.

Revision ID: f4c2a9e7d1b3
Revises: d3b8e5a1f7c2
Create Date: 2026-10-23 09:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f4c2a9e7d1b3"
down_revision: str | None = "d3b8e5a1f7c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RENAMES = (
    ("cassoulet-halal", "cassoulet-poulet-merguez"),
    ("tartiflette-halal", "tartiflette-dinde"),
    ("coquillettes-jambon-fromage", "coquillettes-dinde-fromage"),
)
SQL = sa.text(
    "UPDATE recipes SET slug = :new, updated_at = now() WHERE slug = :old"
    " AND NOT EXISTS (SELECT 1 FROM recipes WHERE slug = :new)"
)


def upgrade() -> None:
    for old, new in RENAMES:
        op.get_bind().execute(SQL, {"old": old, "new": new})


def downgrade() -> None:
    for old, new in RENAMES:
        op.get_bind().execute(SQL, {"old": new, "new": old})
