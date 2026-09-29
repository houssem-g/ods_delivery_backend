"""aurora: the redesigned app's backend additions (2026-09-29)

- couriers: vehicle_model, vehicle_plate, daily_goal, time online (online_since, online_day,
  online_seconds);
- courier_documents: the courier's documents (private uploads) and their review;
- offer_intents: "un livreur prépare une offre…";
- orders: picked_items (the courier's basket), budget_max;
- order_stock_checks: missing_price, quantity;
- messages: photo / voice-note attachments (body may be empty with one);
- hot_deals: price decay (start_price, floor_price, drop_step, drop_every_min); existing deals
  start = floor = price (no decay);
- users.notify_hot_deals (opt-in alerts);
- notification types hot_deal_new, document_verified, document_rejected.

Revision ID: 9a3c5e7f1b20
Revises: 530caff5fa81
Create Date: 2026-09-29 22:45:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "9a3c5e7f1b20"
down_revision: str | None = "530caff5fa81"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_TYPES = (
    "order_accepted", "at_shop", "purchased", "on_the_way", "delivered", "order_cancelled",
    "delivery_cancelled", "delivery_delayed", "eta_update", "new_order", "new_offer", "new_message",
    "emergency_contact", "customer_responded", "customer_no_response_final", "order_confirmed",
    "order_preparing", "hot_deal_reserved", "issue_reported", "account_verified", "account_rejected",
    "stock_check", "stock_check_answered",
)  # fmt: skip
NEW_TYPES = (*OLD_TYPES, "hot_deal_new", "document_verified", "document_rejected")


def _in(values: Sequence[str]) -> str:
    return ",".join(f"'{v}'" for v in values)


def upgrade() -> None:
    # --- couriers -----------------------------------------------------------------------------
    op.add_column("couriers", sa.Column("online_since", sa.DateTime(timezone=True), nullable=True))
    op.add_column("couriers", sa.Column("online_day", sa.Date(), nullable=True))
    op.add_column("couriers", sa.Column("online_seconds", sa.Integer(), server_default="0", nullable=False))
    op.add_column("couriers", sa.Column("vehicle_model", sa.Text(), nullable=True))
    op.add_column("couriers", sa.Column("vehicle_plate", sa.Text(), nullable=True))
    op.add_column("couriers", sa.Column("daily_goal", sa.Numeric(precision=10, scale=3), nullable=True))
    op.create_check_constraint(op.f("ck_couriers_vehicle_model"), "couriers", "length(vehicle_model) <= 60")
    op.create_check_constraint(op.f("ck_couriers_vehicle_plate"), "couriers", "length(vehicle_plate) <= 20")
    op.create_check_constraint(
        op.f("ck_couriers_daily_goal"), "couriers", "daily_goal >= 0 AND daily_goal <= 10000"
    )
    op.create_check_constraint(op.f("ck_couriers_online_seconds"), "couriers", "online_seconds >= 0")
    # couriers online right now start counting from now
    op.execute("UPDATE couriers SET online_since = now() WHERE is_online")

    op.create_table(
        "courier_documents",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("courier_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("file_key", sa.Text(), nullable=False),
        sa.Column("expires_on", sa.Date(), nullable=True),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("reviewed_by", sa.UUID(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "kind IN ('cin','permis','carte_grise','assurance','photo')",
            name=op.f("ck_courier_documents_kind"),
        ),
        sa.CheckConstraint(
            "status IN ('pending','verified','rejected')", name=op.f("ck_courier_documents_status")
        ),
        sa.CheckConstraint("length(note) <= 500", name=op.f("ck_courier_documents_note")),
        sa.ForeignKeyConstraint(
            ["courier_id"],
            ["couriers.id"],
            name=op.f("fk_courier_documents_courier_id_couriers"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by"],
            ["users.id"],
            name=op.f("fk_courier_documents_reviewed_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_courier_documents")),
        sa.UniqueConstraint("courier_id", "kind", name=op.f("uq_courier_documents_courier_id_kind")),
    )
    op.create_index(
        "ix_courier_documents_status_created_at", "courier_documents", ["status", "created_at"], unique=False
    )
    op.execute(
        "CREATE TRIGGER trg_courier_documents_updated_at BEFORE UPDATE ON courier_documents "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )

    # --- offer intents ------------------------------------------------------------------------
    op.create_table(
        "offer_intents",
        sa.Column("order_id", sa.UUID(), nullable=False),
        sa.Column("courier_id", sa.UUID(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["courier_id"],
            ["couriers.id"],
            name=op.f("fk_offer_intents_courier_id_couriers"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"], name=op.f("fk_offer_intents_order_id_orders"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("order_id", "courier_id", name=op.f("pk_offer_intents")),
    )
    op.create_index("ix_offer_intents_updated_at", "offer_intents", ["updated_at"], unique=False)

    # --- orders, stock checks -----------------------------------------------------------------
    op.add_column("orders", sa.Column("budget_max", sa.Numeric(precision=10, scale=3), nullable=True))
    op.add_column("orders", sa.Column("picked_items", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.create_check_constraint(
        op.f("ck_orders_budget_max"), "orders", "budget_max >= 0 AND budget_max <= 2000"
    )
    op.create_check_constraint(
        op.f("ck_orders_picked_items"), "orders", "jsonb_typeof(picked_items) = 'array'"
    )
    op.add_column(
        "order_stock_checks", sa.Column("missing_price", sa.Numeric(precision=10, scale=3), nullable=True)
    )
    op.add_column(
        "order_stock_checks", sa.Column("quantity", sa.Integer(), server_default="1", nullable=False)
    )
    op.create_check_constraint(
        op.f("ck_order_stock_checks_missing_price"),
        "order_stock_checks",
        "missing_price >= 0 AND missing_price <= 2000",
    )
    op.create_check_constraint(
        op.f("ck_order_stock_checks_quantity"), "order_stock_checks", "quantity BETWEEN 1 AND 100"
    )

    # --- messages -----------------------------------------------------------------------------
    op.add_column("messages", sa.Column("attachment_key", sa.Text(), nullable=True))
    op.add_column("messages", sa.Column("attachment_type", sa.Text(), nullable=True))
    op.add_column("messages", sa.Column("attachment_duration", sa.Integer(), nullable=True))
    op.drop_constraint(op.f("ck_messages_body"), "messages", type_="check")
    op.create_check_constraint(
        op.f("ck_messages_body"),
        "messages",
        "length(body) <= 1000 AND (length(body) >= 1 OR attachment_key IS NOT NULL)",
    )
    op.create_check_constraint(
        op.f("ck_messages_attachment_type"), "messages", "attachment_type IN ('image','audio')"
    )
    op.create_check_constraint(
        op.f("ck_messages_attachment_complete"),
        "messages",
        "(attachment_key IS NULL) = (attachment_type IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_messages_attachment_duration"), "messages", "attachment_duration BETWEEN 0 AND 600"
    )

    # --- hot deals ----------------------------------------------------------------------------
    op.add_column("hot_deals", sa.Column("start_price", sa.Numeric(precision=10, scale=3), nullable=True))
    op.add_column("hot_deals", sa.Column("floor_price", sa.Numeric(precision=10, scale=3), nullable=True))
    op.add_column(
        "hot_deals",
        sa.Column("drop_step", sa.Numeric(precision=10, scale=3), server_default="0.500", nullable=False),
    )
    op.add_column("hot_deals", sa.Column("drop_every_min", sa.Integer(), server_default="5", nullable=False))
    op.execute("UPDATE hot_deals SET start_price = price, floor_price = price")  # existing deals: no decay
    op.alter_column("hot_deals", "start_price", nullable=False)
    op.alter_column("hot_deals", "floor_price", nullable=False)
    op.create_check_constraint(
        op.f("ck_hot_deals_floor_price"), "hot_deals", "floor_price >= 0 AND floor_price <= start_price"
    )
    op.create_check_constraint(
        op.f("ck_hot_deals_drop_step"), "hot_deals", "drop_step >= 0 AND drop_step <= 100"
    )
    op.create_check_constraint(
        op.f("ck_hot_deals_drop_every_min"), "hot_deals", "drop_every_min BETWEEN 1 AND 1440"
    )

    # --- users, notification types ------------------------------------------------------------
    op.add_column(
        "users", sa.Column("notify_hot_deals", sa.Boolean(), server_default=sa.text("false"), nullable=False)
    )
    op.create_index(
        "users_hot_deal_alerts", "users", ["id"], unique=False, postgresql_where=sa.text("notify_hot_deals")
    )
    op.drop_constraint(op.f("ck_notifications_type"), "notifications", type_="check")
    op.create_check_constraint(op.f("ck_notifications_type"), "notifications", f"type IN ({_in(NEW_TYPES)})")


def downgrade() -> None:
    op.execute(f"DELETE FROM notifications WHERE type NOT IN ({_in(OLD_TYPES)})")
    op.drop_constraint(op.f("ck_notifications_type"), "notifications", type_="check")
    op.create_check_constraint(op.f("ck_notifications_type"), "notifications", f"type IN ({_in(OLD_TYPES)})")
    op.drop_index("users_hot_deal_alerts", table_name="users", postgresql_where=sa.text("notify_hot_deals"))
    op.drop_column("users", "notify_hot_deals")

    op.drop_constraint(op.f("ck_hot_deals_drop_every_min"), "hot_deals", type_="check")
    op.drop_constraint(op.f("ck_hot_deals_drop_step"), "hot_deals", type_="check")
    op.drop_constraint(op.f("ck_hot_deals_floor_price"), "hot_deals", type_="check")
    op.drop_column("hot_deals", "drop_every_min")
    op.drop_column("hot_deals", "drop_step")
    op.drop_column("hot_deals", "floor_price")
    op.drop_column("hot_deals", "start_price")

    # a message that is only an attachment has no text: keep a placeholder for the old CHECK
    op.execute("UPDATE messages SET body = '📎' WHERE length(body) = 0")
    op.drop_constraint(op.f("ck_messages_attachment_duration"), "messages", type_="check")
    op.drop_constraint(op.f("ck_messages_attachment_complete"), "messages", type_="check")
    op.drop_constraint(op.f("ck_messages_attachment_type"), "messages", type_="check")
    op.drop_constraint(op.f("ck_messages_body"), "messages", type_="check")
    op.create_check_constraint(op.f("ck_messages_body"), "messages", "length(body) BETWEEN 1 AND 1000")
    op.drop_column("messages", "attachment_duration")
    op.drop_column("messages", "attachment_type")
    op.drop_column("messages", "attachment_key")

    op.drop_constraint(op.f("ck_order_stock_checks_quantity"), "order_stock_checks", type_="check")
    op.drop_constraint(op.f("ck_order_stock_checks_missing_price"), "order_stock_checks", type_="check")
    op.drop_column("order_stock_checks", "quantity")
    op.drop_column("order_stock_checks", "missing_price")
    op.drop_constraint(op.f("ck_orders_picked_items"), "orders", type_="check")
    op.drop_constraint(op.f("ck_orders_budget_max"), "orders", type_="check")
    op.drop_column("orders", "picked_items")
    op.drop_column("orders", "budget_max")

    op.drop_index("ix_offer_intents_updated_at", table_name="offer_intents")
    op.drop_table("offer_intents")
    op.execute("DROP TRIGGER trg_courier_documents_updated_at ON courier_documents")
    op.drop_index("ix_courier_documents_status_created_at", table_name="courier_documents")
    op.drop_table("courier_documents")

    op.drop_constraint(op.f("ck_couriers_online_seconds"), "couriers", type_="check")
    op.drop_constraint(op.f("ck_couriers_daily_goal"), "couriers", type_="check")
    op.drop_constraint(op.f("ck_couriers_vehicle_plate"), "couriers", type_="check")
    op.drop_constraint(op.f("ck_couriers_vehicle_model"), "couriers", type_="check")
    op.drop_column("couriers", "daily_goal")
    op.drop_column("couriers", "vehicle_plate")
    op.drop_column("couriers", "vehicle_model")
    op.drop_column("couriers", "online_seconds")
    op.drop_column("couriers", "online_day")
    op.drop_column("couriers", "online_since")
