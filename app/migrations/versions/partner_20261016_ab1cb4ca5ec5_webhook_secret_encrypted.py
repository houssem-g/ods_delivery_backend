"""partners.webhook_secret encrypted at rest (Fernet, PARTNER_SECRET_KEY)

The clear secrets are encrypted with app/security/secret_box.py's scheme and stored as
`fernet:<token>`; the server decrypts to sign, so the signature seen by the partner does not change.
Without a usable key the migration stops before touching anything (outside local/test the settings
already refuse to load without it). Rows already encrypted are left as they are (re-runnable).
Downgrade decrypts back to clear text (same key needed).

Revision ID: ab1cb4ca5ec5
Revises: f2a3b4c5d6e7
Create Date: 2026-10-16 21:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ab1cb4ca5ec5"
down_revision: str | None = "f2a3b4c5d6e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PREFIX = "fernet:"  # app/security/secret_box.py PREFIX

_SECRETS = sa.text("SELECT id, webhook_secret FROM partners WHERE webhook_secret IS NOT NULL")
_SET_SECRET = sa.text("UPDATE partners SET webhook_secret = :secret WHERE id = :id")


def _box():
    """MultiFernet over PARTNER_SECRET_KEY (`new,old` accepted); a clear error without a key."""
    from cryptography.fernet import Fernet, MultiFernet

    from app.config import partner_secret_keys, settings

    keys = partner_secret_keys(settings.PARTNER_SECRET_KEY or "")
    if not keys:
        raise RuntimeError("PARTNER_SECRET_KEY is not set: the partner webhook secrets cannot be encrypted")
    try:
        return MultiFernet([Fernet(k.encode()) for k in keys])
    except ValueError as exc:
        raise RuntimeError("PARTNER_SECRET_KEY is not a valid Fernet key") from exc


def upgrade() -> None:
    box = _box()  # before any change: no key, no migration
    conn = op.get_bind()
    for partner_id, secret in conn.execute(_SECRETS).all():
        if secret.startswith(PREFIX):
            continue  # already encrypted
        token = box.encrypt(secret.encode()).decode()
        conn.execute(_SET_SECRET, {"id": partner_id, "secret": PREFIX + token})


def downgrade() -> None:
    box = _box()
    conn = op.get_bind()
    for partner_id, secret in conn.execute(_SECRETS).all():
        if not secret.startswith(PREFIX):
            continue
        clear = box.decrypt(secret[len(PREFIX) :].encode()).decode()
        conn.execute(_SET_SECRET, {"id": partner_id, "secret": clear})
