"""vitrine requests (17/10/2026): a restaurant asks to have its menu on ODS Delivery

- vitrine_requests: name, phone, address (+ optional position), kind, « Utilisez-vous ODS ? », an
  optional private photo of the menu, a message; source app | public (no account) | ods (sent by
  the ODS backend, with its restaurant id); pending → accepted (points to the shop) | refused (reason).
  One open request per phone number and per proven ODS restaurant (partial unique indexes); ods_verified (the
  ODS call carried the restaurant's own partner key); duplicate_attempts (other tries with that number).
- notifications.type accepts 'vitrine_request' (the requester is told of the decision).
Additive only; downgrade drops the table and the notifications of that type.

Revision ID: 3f8b2c71d9a4
Revises: 7c1e9d2a4b60
Create Date: 2026-10-17 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3f8b2c71d9a4"
down_revision: str | None = "7c1e9d2a4b60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_TYPES = (
    "order_accepted", "at_shop", "purchased", "on_the_way", "delivered", "order_cancelled",
    "delivery_cancelled", "delivery_delayed", "eta_update", "new_order", "new_offer", "new_message",
    "emergency_contact", "customer_responded", "customer_no_response_final", "order_confirmed",
    "order_preparing", "hot_deal_reserved", "issue_reported", "account_verified", "account_rejected",
    "stock_check", "stock_check_answered", "hot_deal_new", "document_verified", "document_rejected",
    "user_reported", "credit_low", "credit_topup_pending", "credit_topup_approved", "credit_topup_rejected",
)  # fmt: skip
NEW_TYPES = (*OLD_TYPES, "vitrine_request")


def _in(values: Sequence[str]) -> str:
    return ",".join(f"'{v}'" for v in values)


def upgrade() -> None:
    op.create_table(
        "vitrine_requests",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=True),
        sa.Column("shop_name", sa.Text(), nullable=False),
        sa.Column("phone", sa.Text(), nullable=False),
        sa.Column("address", sa.Text(), nullable=False),
        sa.Column("lat", sa.Double(), nullable=True),
        sa.Column("lng", sa.Double(), nullable=True),
        sa.Column("kind", sa.Text(), server_default="restaurant", nullable=False),
        sa.Column("uses_ods", sa.Boolean(), nullable=True),
        sa.Column("ods_restaurant_id", sa.BigInteger(), nullable=True),
        sa.Column("ods_verified", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("menu_photo_key", sa.Text(), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("test", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("lang", sa.Text(), server_default="fr", nullable=False),
        sa.Column("follow_token_hash", sa.Text(), nullable=True),
        sa.Column("client_hash", sa.Text(), nullable=True),
        sa.Column("refuse_reason", sa.Text(), nullable=True),
        sa.Column("duplicate_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_duplicate_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.UUID(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("shop_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("status IN ('pending','accepted','refused')", name=op.f("ck_vitrine_requests_status")),
        sa.CheckConstraint("source IN ('app','public','ods')", name=op.f("ck_vitrine_requests_source")),
        sa.CheckConstraint(
            "kind IN ('cafe','restaurant','bakery','supermarket','pharmacy','other')",
            name=op.f("ck_vitrine_requests_kind"),
        ),
        sa.CheckConstraint("length(shop_name) BETWEEN 2 AND 100", name=op.f("ck_vitrine_requests_shop_name")),
        sa.CheckConstraint("length(address) BETWEEN 3 AND 300", name=op.f("ck_vitrine_requests_address")),
        sa.CheckConstraint("length(message) <= 500", name=op.f("ck_vitrine_requests_message")),
        sa.CheckConstraint("length(refuse_reason) <= 300", name=op.f("ck_vitrine_requests_refuse_reason")),
        sa.CheckConstraint("(lat IS NULL) = (lng IS NULL)", name=op.f("ck_vitrine_requests_position")),
        sa.CheckConstraint("duplicate_attempts >= 0", name=op.f("ck_vitrine_requests_duplicate_attempts")),
        sa.CheckConstraint(
            "status <> 'refused' OR refuse_reason IS NOT NULL", name=op.f("ck_vitrine_requests_refused_has_reason")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_vitrine_requests_user_id_users"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["decided_by"], ["users.id"], name=op.f("fk_vitrine_requests_decided_by_users"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["shop_id"], ["shops.id"], name=op.f("fk_vitrine_requests_shop_id_shops"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vitrine_requests")),
        sa.UniqueConstraint("follow_token_hash", name=op.f("uq_vitrine_requests_follow_token_hash")),
    )
    op.create_index(
        "vitrine_requests_open_phone",
        "vitrine_requests",
        ["phone"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "vitrine_requests_open_ods",
        "vitrine_requests",
        ["ods_restaurant_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending' AND ods_restaurant_id IS NOT NULL AND ods_verified"),
    )
    op.create_index(
        "ix_vitrine_requests_status_created_at", "vitrine_requests", ["status", "created_at"], unique=False
    )
    op.create_index("ix_vitrine_requests_user_id", "vitrine_requests", ["user_id"], unique=False)
    op.create_index(
        "ix_vitrine_requests_client_hash_created_at",
        "vitrine_requests",
        ["client_hash", "created_at"],
        unique=False,
    )
    op.drop_constraint(op.f("ck_notifications_type"), "notifications", type_="check")
    op.create_check_constraint(op.f("ck_notifications_type"), "notifications", f"type IN ({_in(NEW_TYPES)})")


def downgrade() -> None:
    op.execute(f"DELETE FROM notifications WHERE type NOT IN ({_in(OLD_TYPES)})")
    op.drop_constraint(op.f("ck_notifications_type"), "notifications", type_="check")
    op.create_check_constraint(op.f("ck_notifications_type"), "notifications", f"type IN ({_in(OLD_TYPES)})")
    op.drop_index("ix_vitrine_requests_client_hash_created_at", table_name="vitrine_requests")
    op.drop_index("ix_vitrine_requests_user_id", table_name="vitrine_requests")
    op.drop_index("ix_vitrine_requests_status_created_at", table_name="vitrine_requests")
    op.drop_index(
        "vitrine_requests_open_ods",
        table_name="vitrine_requests",
        postgresql_where=sa.text("status = 'pending' AND ods_restaurant_id IS NOT NULL AND ods_verified"),
    )
    op.drop_index(
        "vitrine_requests_open_phone",
        table_name="vitrine_requests",
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.drop_table("vitrine_requests")
