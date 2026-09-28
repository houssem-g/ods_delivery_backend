"""MessageLog: WhatsApp / SMS journal (base44/entities/MessageLog.jsonc → `outbound_messages`).

Admins only, read only (Base44: admin for every operation; nothing in the app writes
it). Written by app/services/whatsapp.py.
"""

from typing import Any

from sqlalchemy import false, select, true

from app.compat.registry import EntityDef, LegacyField, register
from app.models import OutboundMessage, User
from app.security.deps import CurrentUser

logs = OutboundMessage.__table__
child = OutboundMessage.__table__.alias("message_log_child")
target = User.__table__.alias("message_log_user")

FALLBACK_LOG_ID = (
    select(child.c.id)
    .where(child.c.parent_id == logs.c.id)
    .order_by(child.c.created_at)
    .limit(1)
    .scalar_subquery()
)


def _read_policy(user: CurrentUser) -> Any:
    return true() if user.is_admin else false()


_SAME_NAME = (
    "channel", "purpose", "template_name", "lang", "idempotency_key", "status", "provider",
    "provider_message_id", "error_code", "error_message", "fallback_status",
)  # fmt: skip
_DATES = ("sent_at", "delivered_at", "read_at", "failed_at", "next_attempt_at", "fallback_deadline_at")

ENTITY = register(
    EntityDef(
        name="MessageLog",
        source=logs.outerjoin(target, target.c.id == logs.c.user_id),
        id_expr=logs.c.id,
        id_type="uuid",
        created_expr=logs.c.created_at,
        updated_expr=logs.c.updated_at,
        fields={
            **{name: LegacyField(logs.c[name], "string") for name in _SAME_NAME},
            **{name: LegacyField(logs.c[name], "datetime") for name in _DATES},
            "params": LegacyField(logs.c.params, "array"),
            "to": LegacyField(logs.c.to_e164, "string"),
            "user_id": LegacyField(target.c.email, "string"),
            "order_id": LegacyField(logs.c.order_id, "id"),
            "notification_id": LegacyField(logs.c.notification_id, "id"),
            "critical": LegacyField(logs.c.critical, "boolean"),
            "attempts": LegacyField(logs.c.attempts, "integer"),
            "parent_log_id": LegacyField(logs.c.parent_id, "id"),
            "fallback_log_id": LegacyField(FALLBACK_LOG_ID, "id"),
        },
        read_policy=_read_policy,
    )
)
