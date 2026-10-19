"""QA campaign 18/10/2026, minors m52 and m38:

- users.notify_delivery_updates / notify_eta_updates / notify_delivery_confirmations (default true): the
  three customer switches « Livreur en route », « Changements d'heure d'arrivée », « Confirmation de
  livraison » were ignored by the server and shown « activés » again (m52);
- notifications.type accepts 'offer_not_retained': the couriers whose offer the customer did not choose
  are told in the app, without a push (m38).

Additive (columns with a default, a wider CHECK); downgrade drops the columns and the new notices.

Revision ID: 4b7e2d9c1a58
Revises: 8e3a6c1f5b27
Create Date: 2026-10-19 14:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "4b7e2d9c1a58"
down_revision: str | None = "8e3a6c1f5b27"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_TYPES = (
    "order_accepted", "at_shop", "purchased", "on_the_way", "delivered", "order_cancelled",
    "delivery_cancelled", "delivery_delayed", "eta_update", "new_order", "new_offer", "new_message",
    "emergency_contact", "customer_responded", "customer_no_response_final", "order_confirmed",
    "order_preparing", "hot_deal_reserved", "issue_reported", "account_verified", "account_rejected",
    "stock_check", "stock_check_answered", "hot_deal_new", "document_verified", "document_rejected",
    "user_reported", "credit_low", "credit_topup_pending", "credit_topup_approved", "credit_topup_rejected",
    "vitrine_request", "pickup_ready",
)  # fmt: skip
NEW_TYPES = (*OLD_TYPES, "offer_not_retained")
COLUMNS = ("notify_delivery_updates", "notify_eta_updates", "notify_delivery_confirmations")


def _in(values: Sequence[str]) -> str:
    return ",".join(f"'{v}'" for v in values)


def upgrade() -> None:
    for name in COLUMNS:
        op.add_column("users", sa.Column(name, sa.Boolean(), server_default=sa.text("true"), nullable=False))
    op.drop_constraint(op.f("ck_notifications_type"), "notifications", type_="check")
    op.create_check_constraint(op.f("ck_notifications_type"), "notifications", f"type IN ({_in(NEW_TYPES)})")


def downgrade() -> None:
    op.execute(f"DELETE FROM notifications WHERE type NOT IN ({_in(OLD_TYPES)})")
    op.drop_constraint(op.f("ck_notifications_type"), "notifications", type_="check")
    op.create_check_constraint(op.f("ck_notifications_type"), "notifications", f"type IN ({_in(OLD_TYPES)})")
    for name in reversed(COLUMNS):
        op.drop_column("users", name)
