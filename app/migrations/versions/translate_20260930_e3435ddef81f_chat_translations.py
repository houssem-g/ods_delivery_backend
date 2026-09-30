"""chat translations: message_translations + translation_usage

- message_translations: a message translated for one reader language (cache, one row per target);
- translation_usage: the monthly spend ledger, checked against TRANSLATE_MONTHLY_BUDGET_USD.

Chat between customers and couriers mixes French, Arabic, Tunisian Derja and Arabizi; a message
is translated on demand (translateOrderMessage) through DigitalOcean Serverless Inference and the
answer is kept, so a message costs at most one call per target language.

Revision ID: e3435ddef81f
Revises: 9a3c5e7f1b20
Create Date: 2026-09-30 06:14:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e3435ddef81f"
down_revision: str | None = "9a3c5e7f1b20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "translation_usage",
        sa.Column("month", sa.Date(), nullable=False),
        sa.Column("calls", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("cost_usd", sa.Numeric(precision=12, scale=6), server_default=sa.text("0"), nullable=False),
        sa.PrimaryKeyConstraint("month", name=op.f("pk_translation_usage")),
    )
    op.create_table(
        "message_translations",
        sa.Column("message_id", sa.UUID(), nullable=False),
        sa.Column("target_lang", sa.Text(), nullable=False),
        sa.Column("translated_text", sa.Text(), nullable=True),
        sa.Column("source_lang", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("cost_usd", sa.Numeric(precision=10, scale=6), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "source_lang IN ('fr','ar','derja','arabizi','other')",
            name=op.f("ck_message_translations_source_lang"),
        ),
        sa.CheckConstraint("target_lang IN ('fr','ar')", name=op.f("ck_message_translations_target_lang")),
        sa.CheckConstraint(
            "length(translated_text) <= 4000", name=op.f("ck_message_translations_translated_text")
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name=op.f("fk_message_translations_message_id_messages"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("message_id", "target_lang", name=op.f("pk_message_translations")),
    )


def downgrade() -> None:
    op.drop_table("message_translations")
    op.drop_table("translation_usage")
