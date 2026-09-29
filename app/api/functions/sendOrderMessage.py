"""sendOrderMessage — post a chat message on an order and notify the other party.

Body { order_id, content, attachment_url?, attachment_type? ('image' | 'audio'),
attachment_duration? (seconds, audio, 0-600) } — attachment_url: the caller's own private upload
(UploadPrivateFile's file_uri, purpose "chat"; image ≤ 8 MB, audio ≤ 3 MB); content may then be
empty. Returns { success, message (+ attachment_url signed, attachment_type, attachment_duration,
read_at), notified }. Errors (JSON `error`): Missing order_id / invalid_content (400, + `max`) /
invalid_attachment / attachment_too_large (+ max_bytes) / invalid_attachment_duration (+ max) (400),
order_not_found (404), not_a_party (403), order_closed (409), too_many_messages (429).
The recipient also gets a realtime signal {kind: "message", order_id, message_id, sender_role}.
Logic: app/services/messages.py."""

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.security.deps import CurrentUser
from app.services import messages


async def handle(
    payload: dict[str, Any], user: CurrentUser | None, session: AsyncSession, request: Request
) -> tuple[int, dict[str, Any]]:
    assert user is not None
    return await messages.send_order_message(session, user, payload)
