"""Message: order chat (base44/entities/Message.jsonc → `messages`).

Read: the sender, the recipient, the two parties of the order (customer, assigned
courier) and admins. Base44 only let the sender (and admins) read a row, the app
reads the chat through getOrderMessages / listMyUnreadMessages anyway.

No direct write: Message create was admin-only on Base44 since 2026-09-27 and the
front's `Message.create` is the legacy fallback of `sendOrderMessage` (refused, 403);
Base44's "sender may update his row" allowed rewriting `recipient_id`, it is gone.
Messages are written by app/services/messages.py only.
"""

from typing import Any

from sqlalchemy import exists, func, or_, select, true

from app.compat.registry import EntityDef, LegacyField, register
from app.models import Courier, Message, Order, User
from app.security.deps import CurrentUser

messages = Message.__table__
orders = Order.__table__
couriers = Courier.__table__
sender = User.__table__.alias("message_sender")
recipient = User.__table__.alias("message_recipient")


def _order_party(user: CurrentUser) -> Any:
    my_couriers = select(couriers.c.id).where(couriers.c.user_id == user.id)
    return exists().where(
        orders.c.id == messages.c.order_id,
        or_(orders.c.customer_id == user.id, orders.c.courier_id.in_(my_couriers)),
    )


def _read_policy(user: CurrentUser) -> Any:
    if user.is_admin:
        return true()
    return or_(messages.c.sender_id == user.id, messages.c.recipient_id == user.id, _order_party(user))


FIELDS: dict[str, LegacyField] = {
    "order_id": LegacyField(messages.c.order_id, "id"),
    "sender_id": LegacyField(sender.c.email, "string"),
    # Base44 wrote '' when a customer's message had no single recipient (open order).
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
