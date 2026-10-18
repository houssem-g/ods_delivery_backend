"""vitrine « Je viens chercher » (18/10/2026): click and collect for a Vitrine linked to ODS

- order_status + 'awaiting_restaurant', 'preparing', 'ready', 'collected' (ALTER TYPE … ADD VALUE, outside the
  transaction): the statuses of an order the customer collects at the counter. No courier path reads them.
- orders: fulfillment ('delivery' default | 'pickup'), pickup_code (3 digits, unique per shop and Tunis
  day), pickup_day, pickup_name, preparing_at, ready_at, collected_at, restaurant_ack_at, ready_notified_at;
  cancelled_by accepts 'restaurant'; CHECKs keep a pickup order in its statuses and a delivery order out of
  them.
- notifications.type accepts 'pickup_ready'.

Downgrade: the pickup orders still open are cancelled (cancelled_by 'system'), the collected ones become
'cancelled' too (no other status of the previous schema fits an order without a courier), the 'restaurant'
cancellations become 'system', the 'pickup_ready' notices go, then the columns and CHECKs go. PostgreSQL
cannot drop an enum value: the four labels stay in the type, unused (the initial migration's downgrade drops
the whole type).

Revision ID: 6a1d4e8b2f37
Revises: 9c4e2b7a1d35
Create Date: 2026-10-18 09:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "6a1d4e8b2f37"
down_revision: str | None = "9c4e2b7a1d35"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PICKUP = ("awaiting_restaurant", "preparing", "ready", "collected")
OLD_TYPES = (
    "order_accepted", "at_shop", "purchased", "on_the_way", "delivered", "order_cancelled",
    "delivery_cancelled", "delivery_delayed", "eta_update", "new_order", "new_offer", "new_message",
    "emergency_contact", "customer_responded", "customer_no_response_final", "order_confirmed",
    "order_preparing", "hot_deal_reserved", "issue_reported", "account_verified", "account_rejected",
    "stock_check", "stock_check_answered", "hot_deal_new", "document_verified", "document_rejected",
    "user_reported", "credit_low", "credit_topup_pending", "credit_topup_approved", "credit_topup_rejected",
    "vitrine_request",
)  # fmt: skip
NEW_TYPES = (*OLD_TYPES, "pickup_ready")
OLD_CANCELLED_BY = "cancelled_by IN ('customer','courier','admin','system')"
NEW_CANCELLED_BY = "cancelled_by IN ('customer','courier','admin','system','restaurant')"


def _in(values: Sequence[str]) -> str:
    return ",".join(f"'{v}'" for v in values)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for value in PICKUP:
            op.execute(f"ALTER TYPE order_status ADD VALUE IF NOT EXISTS '{value}'")
    op.add_column("orders", sa.Column("fulfillment", sa.Text(), server_default="delivery", nullable=False))
    op.add_column("orders", sa.Column("pickup_code", sa.Text(), nullable=True))
    op.add_column("orders", sa.Column("pickup_day", sa.Date(), nullable=True))
    op.add_column("orders", sa.Column("pickup_name", sa.Text(), nullable=True))
    for name in ("preparing_at", "ready_at", "collected_at", "restaurant_ack_at", "ready_notified_at"):
        op.add_column("orders", sa.Column(name, sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        op.f("ck_orders_fulfillment"), "orders", "fulfillment IN ('delivery','pickup')"
    )
    op.create_check_constraint(
        op.f("ck_orders_pickup_vitrine"), "orders", "fulfillment = 'delivery' OR source = 'vitrine'"
    )
    op.create_check_constraint(
        op.f("ck_orders_delivery_statuses"),
        "orders",
        f"fulfillment = 'pickup' OR status NOT IN ({_in(PICKUP)})",
    )
    op.create_check_constraint(
        op.f("ck_orders_pickup_statuses"),
        "orders",
        f"fulfillment = 'delivery' OR status IN ({_in((*PICKUP, 'cancelled'))})",
    )
    op.create_check_constraint(
        op.f("ck_orders_collected_at"), "orders", "status <> 'collected' OR collected_at IS NOT NULL"
    )
    op.create_check_constraint(
        op.f("ck_orders_pickup_code"), "orders", "pickup_code IS NULL OR pickup_code ~ '^[0-9]{3}$'"
    )
    op.create_index(
        "orders_pickup_code",
        "orders",
        ["vitrine_shop_id", "pickup_day", "pickup_code"],
        unique=True,
        postgresql_where=sa.text("pickup_code IS NOT NULL"),
    )
    op.drop_constraint(op.f("ck_orders_cancelled_by"), "orders", type_="check")
    op.create_check_constraint(op.f("ck_orders_cancelled_by"), "orders", NEW_CANCELLED_BY)
    op.drop_constraint(op.f("ck_notifications_type"), "notifications", type_="check")
    op.create_check_constraint(op.f("ck_notifications_type"), "notifications", f"type IN ({_in(NEW_TYPES)})")


def downgrade() -> None:
    op.execute(
        "UPDATE orders SET status = 'cancelled', cancelled_at = coalesce(cancelled_at, now()), "
        "cancelled_by = coalesce(cancelled_by, 'system'), cancel_reason = coalesce(cancel_reason, 'pickup_removed') "
        f"WHERE status::text IN ({_in(PICKUP)})"
    )
    op.execute(
        f"UPDATE order_status_events SET to_status = 'cancelled' WHERE to_status::text IN ({_in(PICKUP)})"
    )
    op.execute(
        f"UPDATE order_status_events SET from_status = NULL WHERE from_status::text IN ({_in(PICKUP)})"
    )
    op.execute("UPDATE orders SET cancelled_by = 'system' WHERE cancelled_by = 'restaurant'")
    op.execute(f"DELETE FROM notifications WHERE type NOT IN ({_in(OLD_TYPES)})")
    op.drop_constraint(op.f("ck_notifications_type"), "notifications", type_="check")
    op.create_check_constraint(op.f("ck_notifications_type"), "notifications", f"type IN ({_in(OLD_TYPES)})")
    op.drop_constraint(op.f("ck_orders_cancelled_by"), "orders", type_="check")
    op.create_check_constraint(op.f("ck_orders_cancelled_by"), "orders", OLD_CANCELLED_BY)
    op.drop_index("orders_pickup_code", table_name="orders")
    for name in (
        "pickup_code",
        "collected_at",
        "pickup_statuses",
        "delivery_statuses",
        "pickup_vitrine",
        "fulfillment",
    ):
        op.drop_constraint(op.f(f"ck_orders_{name}"), "orders", type_="check")
    for name in (
        "ready_notified_at", "restaurant_ack_at", "collected_at", "ready_at", "preparing_at", "pickup_name",
        "pickup_day", "pickup_code", "fulfillment",
    ):  # fmt: skip
        op.drop_column("orders", name)
