"""Message: order chat (`messages`).

Read: the sender and admins (`data.sender_id == user.email` or
admin). The app never reads this entity: the chat goes through getOrderMessages /
listMyUnreadMessages, which check the order's parties themselves. A wider rule (recipient,
parties) would hand the other party's rows to any REST or realtime subscriber for nothing
(security-published / realtime-rules specs).

No direct write: the
front's `Message.create` is the legacy fallback of `sendOrderMessage` (refused, 403).
Messages are written by app/services/messages.py only.
"""

from typing import Any

from sqlalchemy import func, true

from app.compat.registry import EntityDef, LegacyField, register
from app.models import Message, User
from app.security.deps import CurrentUser

messages = Message.__table__
sender = User.__table__.alias("message_sender")
recipient = User.__table__.alias("message_recipient")


def _read_policy(user: CurrentUser) -> Any:
    if user.is_admin:
        return true()
    return messages.c.sender_id == user.id


FIELDS: dict[str, LegacyField] = {
    "order_id": LegacyField(messages.c.order_id, "id"),
    "sender_id": LegacyField(sender.c.email, "string"),
    # '' when a customer's message had no single recipient (open order).
    "recipient_id": LegacyField(func.coalesce(recipient.c.email, ""), "string"),
    "sender_role": LegacyField(messages.c.sender_role, "string"),
    "content": LegacyField(messages.c.body, "string"),
    "is_template": LegacyField(messages.c.is_template, "boolean"),
    "is_read": LegacyField(messages.c.read_at.is_not(None), "boolean"),
}

ENTITY = register(
    EntityDef(
        name="Message",
        source=messages.outerjoin(sender, sender.c.id == messages.c.sender_id).outerjoin(
            recipient, recipient.c.id == messages.c.recipient_id
        ),
        id_expr=messages.c.id,
        id_type="uuid",
        created_expr=messages.c.created_at,
        updated_expr=messages.c.updated_at,
        created_by_expr=sender.c.email,
        fields=FIELDS,
        read_policy=_read_policy,
    )
)
