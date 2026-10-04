"""blocking and reporting users (App Review 1.2: user-generated content in the order chat)

- user_blocks: one row per (blocker, blocked); a block hides both users from each other;
- user_reports: a user reports another (optionally one chat message) to the admins;
- notification type user_reported (to the admins).

Revision ID: 5b1c0d7e9a42
Revises: e3435ddef81f
Create Date: 2026-10-04 22:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5b1c0d7e9a42"
down_revision: str | None = "e3435ddef81f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_TYPES = (
    "order_accepted", "at_shop", "purchased", "on_the_way", "delivered", "order_cancelled",
    "delivery_cancelled", "delivery_delayed", "eta_update", "new_order", "new_offer", "new_message",
    "emergency_contact", "customer_responded", "customer_no_response_final", "order_confirmed",
    "order_preparing", "hot_deal_reserved", "issue_reported", "account_verified", "account_rejected",
    "stock_check", "stock_check_answered", "hot_deal_new", "document_verified", "document_rejected",
)  # fmt: skip
NEW_TYPES = (*OLD_TYPES, "user_reported")


def _in(values: Sequence[str]) -> str:
    return ",".join(f"'{v}'" for v in values)


def upgrade() -> None:
    op.create_table(
        "user_blocks",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("blocker_id", sa.UUID(), nullable=False),
        sa.Column("blocked_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("blocker_id <> blocked_id", name=op.f("ck_user_blocks_not_self")),
        sa.ForeignKeyConstraint(
            ["blocker_id"], ["users.id"], name=op.f("fk_user_blocks_blocker_id_users"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["blocked_id"], ["users.id"], name=op.f("fk_user_blocks_blocked_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_blocks")),
    )
    op.create_index("user_blocks_pair", "user_blocks", ["blocker_id", "blocked_id"], unique=True)
    op.create_index("ix_user_blocks_blocked_id", "user_blocks", ["blocked_id"], unique=False)

    op.create_table(
        "user_reports",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("reporter_id", sa.UUID(), nullable=True),
        sa.Column("reported_id", sa.UUID(), nullable=True),
        sa.Column("order_id", sa.UUID(), nullable=True),
        sa.Column("message_id", sa.UUID(), nullable=True),
        sa.Column("message_excerpt", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default="open", nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "reason IN ('harassment','inappropriate','spam','fraud','dangerous','other')",
            name=op.f("ck_user_reports_reason"),
        ),
        sa.CheckConstraint("status IN ('open','resolved','dismissed')", name=op.f("ck_user_reports_status")),
        sa.ForeignKeyConstraint(
            ["reporter_id"], ["users.id"], name=op.f("fk_user_reports_reporter_id_users"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["reported_id"], ["users.id"], name=op.f("fk_user_reports_reported_id_users"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"], name=op.f("fk_user_reports_order_id_orders"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name=op.f("fk_user_reports_message_id_messages"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["resolved_by"], ["users.id"], name=op.f("fk_user_reports_resolved_by_users"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_reports")),
    )
    op.create_index(
        "user_reports_open", "user_reports", ["created_at"], unique=False,
        postgresql_where=sa.text("status = 'open'"),
    )  # fmt: skip
    op.create_index("ix_user_reports_reported_id", "user_reports", ["reported_id"], unique=False)

    op.drop_constraint(op.f("ck_notifications_type"), "notifications", type_="check")
    op.create_check_constraint(op.f("ck_notifications_type"), "notifications", f"type IN ({_in(NEW_TYPES)})")


def downgrade() -> None:
    op.execute(f"DELETE FROM notifications WHERE type NOT IN ({_in(OLD_TYPES)})")
    op.drop_constraint(op.f("ck_notifications_type"), "notifications", type_="check")
    op.create_check_constraint(op.f("ck_notifications_type"), "notifications", f"type IN ({_in(OLD_TYPES)})")
    op.drop_index("ix_user_reports_reported_id", table_name="user_reports")
    op.drop_index("user_reports_open", table_name="user_reports", postgresql_where=sa.text("status = 'open'"))
    op.drop_table("user_reports")
    op.drop_index("ix_user_blocks_blocked_id", table_name="user_blocks")
    op.drop_index("user_blocks_pair", table_name="user_blocks")
    op.drop_table("user_blocks")
