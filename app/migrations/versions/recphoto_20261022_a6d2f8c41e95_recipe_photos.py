"""recipe photos and the two parts of the time (22/10/2026, owner: Recettes redesign with dish photos)

recipes gets its photo: the bucket keys of the photo (`photo_key`, ~960 px WebP for the recipe sheet) and of
its small version (`photo_thumb_key`, ~480 px for the cards, optional), the alternative text fr / ar, where it
comes from (`photo_source`: 'ia' = generated, 'photo' = a real one), its credit (`photo_credit` JSONB:
author, licence, links — shown when the licence asks for it) and when it was set. And the two parts of
`time_min` from the recipe files (`prep_min`, `cook_min`, filled by the next recipe import): the time ring of
the recipe sheet. The files live in the
PUBLIC part of the ODS Delivery bucket (`public/recipes/<slug>.webp`), served like the Vitrine thumbnails
(app/services/vitrine_thumbs.py, s3.public_url). A recipe without a photo keeps the design A tile.

Additive only: nullable columns, no row changes; the recipe import never touches the photo columns. Downgrade drops the columns.

Revision ID: a6d2f8c41e95
Revises: e7a3c9d1b4f6
Create Date: 2026-10-22 00:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a6d2f8c41e95"
down_revision: str | None = "e7a3c9d1b4f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("recipes", sa.Column("photo_key", sa.Text(), nullable=True))
    op.add_column("recipes", sa.Column("photo_thumb_key", sa.Text(), nullable=True))
    op.add_column("recipes", sa.Column("photo_alt_fr", sa.Text(), nullable=True))
    op.add_column("recipes", sa.Column("photo_alt_ar", sa.Text(), nullable=True))
    op.add_column("recipes", sa.Column("photo_source", sa.Text(), nullable=True))
    op.add_column("recipes", sa.Column("photo_set_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("recipes", sa.Column("photo_credit", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("recipes", sa.Column("prep_min", sa.SmallInteger(), nullable=True))
    op.add_column("recipes", sa.Column("cook_min", sa.SmallInteger(), nullable=True))
    op.create_check_constraint(op.f("ck_recipes_prep_min"), "recipes", "prep_min IS NULL OR prep_min BETWEEN 0 AND 1440")
    op.create_check_constraint(op.f("ck_recipes_cook_min"), "recipes", "cook_min IS NULL OR cook_min BETWEEN 0 AND 1440")
    op.create_check_constraint(
        op.f("ck_recipes_photo_source"), "recipes", "photo_source IS NULL OR photo_source IN ('ia','photo')"
    )
    op.create_check_constraint(
        op.f("ck_recipes_photo"),
        "recipes",
        "(photo_key IS NULL) = (photo_source IS NULL) AND (photo_thumb_key IS NULL OR photo_key IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_recipes_cook_min"), "recipes", type_="check")
    op.drop_constraint(op.f("ck_recipes_prep_min"), "recipes", type_="check")
    op.drop_constraint(op.f("ck_recipes_photo"), "recipes", type_="check")
    op.drop_constraint(op.f("ck_recipes_photo_source"), "recipes", type_="check")
    for column in (
        "cook_min",
        "prep_min",
        "photo_credit",
        "photo_set_at",
        "photo_source",
        "photo_alt_ar",
        "photo_alt_fr",
        "photo_thumb_key",
        "photo_key",
    ):
        op.drop_column("recipes", column)
