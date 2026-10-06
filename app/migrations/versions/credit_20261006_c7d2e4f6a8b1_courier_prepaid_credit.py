"""courier prepaid credit (decision D-10: the commission is paid in advance)

- courier_ledger_entries.kind gains credit_topup / credit_bonus / credit_prime (negative amounts);
- credit_topups: a bank-counter deposit (receipt photo, admin approval) or cash at a cashier;
- credit_cashiers: the users allowed to sell credit for cash (the ODS café in Sousse);
- notification types credit_low / credit_topup_pending / credit_topup_approved / credit_topup_rejected.

Revision ID: c7d2e4f6a8b1
Revises: 5b1c0d7e9a42
Create Date: 2026-10-06 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c7d2e4f6a8b1"
down_revision: str | None = "5b1c0d7e9a42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_KINDS = (
    "commission_due", "commission_waived_launch", "commission_waived_quota", "payment_received", "adjustment",
)  # fmt: skip
NEW_KINDS = (*OLD_KINDS, "credit_topup", "credit_bonus", "credit_prime")
OLD_TYPES = (
    "order_accepted", "at_shop", "purchased", "on_the_way", "delivered", "order_cancelled",
    "delivery_cancelled", "delivery_delayed", "eta_update", "new_order", "new_offer", "new_message",
    "emergency_contact", "customer_responded", "customer_no_response_final", "order_confirmed",
    "order_preparing", "hot_deal_reserved", "issue_reported", "account_verified", "account_rejected",
    "stock_check", "stock_check_answered", "hot_deal_new", "document_verified", "document_rejected",
    "user_reported",
)  # fmt: skip
NEW_TYPES = (*OLD_TYPES, "credit_low", "credit_topup_pending", "credit_topup_approved", "credit_topup_rejected")


def _in(values: Sequence[str]) -> str:
    return ",".join(f"'{v}'" for v in values)


def upgrade() -> None:
    op.drop_constraint(op.f("ck_courier_ledger_entries_kind"), "courier_ledger_entries", type_="check")
    op.create_check_constraint(
        op.f("ck_courier_ledger_entries_kind"), "courier_ledger_entries", f"kind IN ({_in(NEW_KINDS)})"
    )

    op.create_table(
        "credit_cashiers",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("address", sa.Text(), nullable=True),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_credit_cashiers_user_id_users"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], name=op.f("fk_credit_cashiers_created_by_users")),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_credit_cashiers")),
    )

    op.create_table(
        "credit_topups",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("courier_id", sa.UUID(), nullable=False),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("amount", sa.Numeric(10, 3), nullable=False),
        sa.Column("bonus", sa.Numeric(10, 3), server_default="0", nullable=False),
        sa.Column("receipt_key", sa.Text(), nullable=True),
        sa.Column("reference", sa.Text(), nullable=True),
        sa.Column("cashier_user_id", sa.UUID(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("reviewed_by", sa.UUID(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("remitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("method IN ('bank_deposit','cashier')", name=op.f("ck_credit_topups_method")),
        sa.CheckConstraint("status IN ('pending','approved','rejected')", name=op.f("ck_credit_topups_status")),
        sa.CheckConstraint("amount > 0 AND amount <= 1000", name=op.f("ck_credit_topups_amount")),
        sa.CheckConstraint("bonus >= 0", name=op.f("ck_credit_topups_bonus")),
        sa.CheckConstraint(
            "method <> 'cashier' OR cashier_user_id IS NOT NULL", name=op.f("ck_credit_topups_cashier")
        ),
        sa.ForeignKeyConstraint(
            ["courier_id"], ["couriers.id"], name=op.f("fk_credit_topups_courier_id_couriers"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["cashier_user_id"],
            ["users.id"],
            name=op.f("fk_credit_topups_cashier_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["reviewed_by"], ["users.id"], name=op.f("fk_credit_topups_reviewed_by_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_credit_topups")),
    )
    op.create_index(
        "ix_credit_topups_courier_id_created_at", "credit_topups", ["courier_id", "created_at"], unique=False
    )
    op.create_index(
        "credit_topups_pending", "credit_topups", ["created_at"], unique=False,
        postgresql_where=sa.text("status = 'pending'"),
    )  # fmt: skip
    op.create_index(
        "credit_topups_unremitted", "credit_topups", ["cashier_user_id"], unique=False,
        postgresql_where=sa.text("method = 'cashier' AND status = 'approved' AND remitted_at IS NULL"),
    )  # fmt: skip

    op.drop_constraint(op.f("ck_notifications_type"), "notifications", type_="check")
    op.create_check_constraint(op.f("ck_notifications_type"), "notifications", f"type IN ({_in(NEW_TYPES)})")


def downgrade() -> None:
    op.execute(f"DELETE FROM notifications WHERE type NOT IN ({_in(OLD_TYPES)})")
    op.drop_constraint(op.f("ck_notifications_type"), "notifications", type_="check")
    op.create_check_constraint(op.f("ck_notifications_type"), "notifications", f"type IN ({_in(OLD_TYPES)})")
    op.drop_index(
        "credit_topups_unremitted", table_name="credit_topups",
        postgresql_where=sa.text("method = 'cashier' AND status = 'approved' AND remitted_at IS NULL"),
    )  # fmt: skip
    op.drop_index("credit_topups_pending", table_name="credit_topups", postgresql_where=sa.text("status = 'pending'"))
    op.drop_index("ix_credit_topups_courier_id_created_at", table_name="credit_topups")
    op.drop_table("credit_topups")
    op.drop_table("credit_cashiers")
    op.execute(f"DELETE FROM courier_ledger_entries WHERE kind NOT IN ({_in(OLD_KINDS)})")
    op.drop_constraint(op.f("ck_courier_ledger_entries_kind"), "courier_ledger_entries", type_="check")
    op.create_check_constraint(
        op.f("ck_courier_ledger_entries_kind"), "courier_ledger_entries", f"kind IN ({_in(OLD_KINDS)})"
    )
