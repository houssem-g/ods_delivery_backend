"""Owner 21/10/2026: « Relancer » a courier for his documents — notifications.type accepts
'documents_requested' (in-app + push, a tap opens « Mes documents »). The reminders themselves are
audit_log rows (entity Courier, action documents_requested): no new table.

Additive (a wider CHECK); downgrade deletes those notices and narrows the CHECK back.

Revision ID: e7a3c9d1b4f6
Revises: c4e8a2f61d39
Create Date: 2026-10-21 22:30:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "e7a3c9d1b4f6"
down_revision: str | None = "c4e8a2f61d39"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_TYPES = (
    "order_accepted", "at_shop", "purchased", "on_the_way", "delivered", "order_cancelled",
    "delivery_cancelled", "delivery_delayed", "eta_update", "new_order", "new_offer", "new_message",
    "emergency_contact", "customer_responded", "customer_no_response_final", "order_confirmed",
    "order_preparing", "hot_deal_reserved", "issue_reported", "account_verified", "account_rejected",
    "stock_check", "stock_check_answered", "hot_deal_new", "document_verified", "document_rejected",
    "user_reported", "credit_low", "credit_topup_pending", "credit_topup_approved", "credit_topup_rejected",
    "vitrine_request", "pickup_ready", "offer_not_retained",
)  # fmt: skip
NEW_TYPES = (*OLD_TYPES, "documents_requested")


def _in(values: Sequence[str]) -> str:
    return ",".join(f"'{v}'" for v in values)


def upgrade() -> None:
    op.drop_constraint(op.f("ck_notifications_type"), "notifications", type_="check")
    op.create_check_constraint(op.f("ck_notifications_type"), "notifications", f"type IN ({_in(NEW_TYPES)})")


def downgrade() -> None:
    op.execute(f"DELETE FROM notifications WHERE type NOT IN ({_in(OLD_TYPES)})")
    op.drop_constraint(op.f("ck_notifications_type"), "notifications", type_="check")
    op.create_check_constraint(op.f("ck_notifications_type"), "notifications", f"type IN ({_in(OLD_TYPES)})")
