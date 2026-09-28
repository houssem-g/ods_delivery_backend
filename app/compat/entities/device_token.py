"""DeviceToken: push tokens (base44/entities/DeviceToken.jsonc → `device_tokens`).

Read: the owner and admins (Base44: admins only; the app never reads it, the owner
seeing his own devices leaks nothing). No direct write: registerDeviceToken /
unregisterDeviceToken and the push service maintain the rows.

`endpoint_hash` (Base44's dedupe key, sha256(token) truncated to 32 hex chars) is
derived; `provider` is always `fcm`.
"""

from typing import Any

from sqlalchemy import String, func, literal, true

from app.compat.registry import EntityDef, LegacyField, register
from app.models import DeviceToken, User
from app.security.deps import CurrentUser

tokens = DeviceToken.__table__
owner = User.__table__.alias("device_token_owner")

ENDPOINT_HASH = func.substr(func.encode(func.sha256(func.convert_to(tokens.c.token, "UTF8")), "hex"), 1, 32)


def _read_policy(user: CurrentUser) -> Any:
    return true() if user.is_admin else tokens.c.user_id == user.id


ENTITY = register(
    EntityDef(
        name="DeviceToken",
        source=tokens.join(owner, owner.c.id == tokens.c.user_id),
        id_expr=tokens.c.id,
        id_type="uuid",
        created_expr=tokens.c.created_at,
        updated_expr=tokens.c.updated_at,
        fields={
            "user_id": LegacyField(owner.c.email, "string"),
            "token": LegacyField(tokens.c.token, "string"),
            "endpoint_hash": LegacyField(ENDPOINT_HASH, "string"),
            "platform": LegacyField(tokens.c.platform, "string"),
            "provider": LegacyField(literal("fcm", String), "string"),
            "app_version": LegacyField(tokens.c.app_version, "string"),
            "device_model": LegacyField(tokens.c.device_model, "string"),
            "locale": LegacyField(tokens.c.locale, "string"),
            "is_active": LegacyField(tokens.c.is_active, "boolean"),
            "last_seen_at": LegacyField(tokens.c.last_seen_at, "datetime"),
            "last_error": LegacyField(tokens.c.last_error, "string"),
            "failure_count": LegacyField(tokens.c.failure_count, "integer"),
        },
        read_policy=_read_policy,
    )
)
