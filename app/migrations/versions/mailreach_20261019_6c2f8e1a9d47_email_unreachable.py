"""Sign-up e-mail reachability (owner 19/10/2026): users.email_unreachable_at / email_unreachable_reason,
set when the mail server refuses the address of a code at once (RCPT TO 5xx), shown on the code screen
(« Nous n'arrivons pas à envoyer d'email à cette adresse »), cleared by the next accepted send.

Additive (two nullable columns); downgrade drops them.

Revision ID: 6c2f8e1a9d47
Revises: 4b7e2d9c1a58
Create Date: 2026-10-19 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "6c2f8e1a9d47"
down_revision: str | None = "4b7e2d9c1a58"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("email_unreachable_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("email_unreachable_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "email_unreachable_reason")
    op.drop_column("users", "email_unreachable_at")
