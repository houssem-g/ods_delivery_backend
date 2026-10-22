"""recipe videos (22/10/2026, owner: videos linked to the steps of the Recettes — « la préparation n'est pas
toujours claire »)

- recipe_gestures: the library of basic gestures (« faire revenir l'oignon », « plier une brik »…): a short
  clip of ours on the public bucket (public/recipes/gestures/<key>.webm, .mp4 and .webp next to it) or a
  YouTube passage (id + start / end, played by YouTube's own player), its names fr / ar, its credit, and the
  trigger words fr / ar that link it automatically to every step that says it;
- recipe_step_videos: the YouTube passage of one step of a recipe (recipe, 1-based step, id, start / end,
  title and channel of the video).

Additive only: two new tables, no existing table or row changes; the recipe import never touches them.
Filled by scripts/import_recipe_videos.py (or the admin function importRecipeVideos) from
data/recipes/videos.json. Downgrade drops the two tables.

Revision ID: d3b8e5a1f7c2
Revises: a6d2f8c41e95
Create Date: 2026-10-22 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d3b8e5a1f7c2"
down_revision: str | None = "a6d2f8c41e95"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPDATED_AT = ("recipe_gestures", "recipe_step_videos")
YT = "^[A-Za-z0-9_-]{11}$"


def upgrade() -> None:
    op.create_table(
        "recipe_gestures",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("name_fr", sa.Text(), nullable=False),
        sa.Column("name_ar", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("clip_key", sa.Text(), nullable=True),
        sa.Column("youtube_id", sa.Text(), nullable=True),
        sa.Column("start_s", sa.Integer(), nullable=True),
        sa.Column("end_s", sa.Integer(), nullable=True),
        sa.Column("credit", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("triggers_fr", postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False),
        sa.Column("triggers_ar", postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False),
        sa.Column("position", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("kind IN ('clip','youtube')", name=op.f("ck_recipe_gestures_kind")),
        sa.CheckConstraint(
            "(kind = 'clip') = (clip_key IS NOT NULL) AND (kind = 'youtube') = (youtube_id IS NOT NULL)",
            name=op.f("ck_recipe_gestures_source"),
        ),
        sa.CheckConstraint(f"youtube_id IS NULL OR youtube_id ~ '{YT}'", name=op.f("ck_recipe_gestures_youtube_id")),
        sa.CheckConstraint(
            "youtube_id IS NULL OR (start_s >= 0 AND end_s > start_s AND end_s - start_s <= 600)",
            name=op.f("ck_recipe_gestures_passage"),
        ),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_recipe_gestures")),
    )
    op.create_table(
        "recipe_step_videos",
        sa.Column("recipe_id", sa.UUID(), nullable=False),
        sa.Column("step", sa.SmallInteger(), nullable=False),
        sa.Column("youtube_id", sa.Text(), nullable=False),
        sa.Column("start_s", sa.Integer(), nullable=False),
        sa.Column("end_s", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("channel", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("step BETWEEN 1 AND 60", name=op.f("ck_recipe_step_videos_step")),
        sa.CheckConstraint(f"youtube_id ~ '{YT}'", name=op.f("ck_recipe_step_videos_youtube_id")),
        sa.CheckConstraint(
            "start_s >= 0 AND end_s > start_s AND end_s - start_s <= 600", name=op.f("ck_recipe_step_videos_passage")
        ),
        sa.ForeignKeyConstraint(
            ["recipe_id"], ["recipes.id"], name=op.f("fk_recipe_step_videos_recipe_id_recipes"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("recipe_id", "step", name="pk_recipe_step_videos"),
    )
    for table in UPDATED_AT:
        op.execute(
            f"CREATE TRIGGER trg_{table}_updated_at BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
        )


def downgrade() -> None:
    for table in UPDATED_AT:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_updated_at ON {table}")
    op.drop_table("recipe_step_videos")
    op.drop_table("recipe_gestures")
