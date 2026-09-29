"""signalTyping — "… est en train d'écrire" in the order chat (app/services/messages.py).

Body { order_id }: the customer or the assigned courier of a live order. The other party's
subscribers of the Message entity receive {entity: "Message", type: "signal", id: <order_id>,
data: {kind: "typing", order_id, sender_role}, timestamp}; nobody else (no DB row).
Returns { success }. Errors: Missing order_id (400), not_a_party (403), order_not_found (404),
order_not_live (409, + status), too_many_typing_signals (429, 20 per minute per user).
"""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.rate_limit import allow
from app.security.deps import CurrentUser
from app.services import messages

RATE = "20/minute"


async def handle(
    payload: dict[str, Any], user: CurrentUser | None, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    assert user is not None
    if not allow("signalTyping", f"user:{user.id}", RATE):
        return 429, {"error": "too_many_typing_signals", "limit": RATE}
    return await messages.signal_typing(session, user, payload)
