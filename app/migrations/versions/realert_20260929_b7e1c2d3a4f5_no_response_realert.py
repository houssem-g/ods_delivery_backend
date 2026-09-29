"""no-response: a second (last) urgent alert once the deadline passed (owner's request, 2026-09-29)

The expired case is closed as 'realerted' and a new case starts the countdown again.

Revision ID: b7e1c2d3a4f5
Revises: 6691f4426902
Create Date: 2026-09-29 13:30:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "b7e1c2d3a4f5"
down_revision: str | None = "6691f4426902"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD = (
    "customer_confirmed", "courier_reached", "resold", "cancelled_kept", "returned_to_shop",
    "auto_closed", "courier_cancelled_other", "delivered", "order_cancelled",
)  # fmt: skip
NEW = (*OLD, "realerted")


def _check(values: tuple[str, ...]) -> str:
    return "resolution IN (" + ",".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_no_response_cases_resolution"), "no_response_cases", type_="check")
    op.create_check_constraint(op.f("ck_no_response_cases_resolution"), "no_response_cases", _check(NEW))


def downgrade() -> None:
    op.execute("UPDATE no_response_cases SET resolution = 'customer_confirmed' WHERE resolution = 'realerted'")
    op.drop_constraint(op.f("ck_no_response_cases_resolution"), "no_response_cases", type_="check")
    op.create_check_constraint(op.f("ck_no_response_cases_resolution"), "no_response_cases", _check(OLD))
