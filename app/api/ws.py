"""GET /api/ws?token=<access token> — realtime subscriptions (ARCHITECTURE §7).

client -> {"op":"subscribe","entity":"Order"} | {"op":"unsubscribe","entity":"Order"} | {"op":"ping"}
server -> {"entity","type","id","data"?} | {"op":"subscribed"|"unsubscribed","entity"} | {"op":"pong"}
          | {"op":"error","error":...} | {"type":"resync"}
Close codes: 4401 = missing/invalid/expired token (the client refreshes and reconnects).
"""

import asyncio
import contextlib
import json
from datetime import UTC, datetime

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.compat.registry import get_entity
from app.db import SessionLocal
from app.realtime.hub import hub
from app.security.deps import user_from_token
from app.security.tokens import InvalidToken, decode_access_token

router = APIRouter()
CLOSE_UNAUTHORIZED = 4401
MAX_SUBSCRIPTIONS = 32


async def _close_at_expiry(websocket: WebSocket, expires_at: float) -> None:
    await asyncio.sleep(max(0.0, expires_at - datetime.now(UTC).timestamp()))
    with contextlib.suppress(Exception):
        await websocket.close(code=CLOSE_UNAUTHORIZED, reason="token expired")


@router.websocket("/api/ws")
async def websocket_endpoint(websocket: WebSocket, token: str | None = None) -> None:
    await websocket.accept()
    try:
        claims = decode_access_token(token or "")
    except InvalidToken:
        await websocket.close(code=CLOSE_UNAUTHORIZED, reason="invalid token")
        return
    async with SessionLocal() as session:
        user = await user_from_token(session, token or "")
    if user is None:
        await websocket.close(code=CLOSE_UNAUTHORIZED, reason="invalid token")
        return

    subscriber = hub.add(websocket, user)
    expiry = asyncio.create_task(_close_at_expiry(websocket, float(claims["exp"])))
    try:
        while True:
            try:
                message = json.loads(await websocket.receive_text())
            except ValueError:
                await websocket.send_json({"op": "error", "error": "invalid_json", "message": "Invalid JSON"})
                continue
            op = message.get("op") if isinstance(message, dict) else None
            entity = message.get("entity") if isinstance(message, dict) else None
            if op == "ping":
                await websocket.send_json({"op": "pong"})
            elif op == "subscribe":
                if not isinstance(entity, str) or get_entity(entity) is None:
                    await websocket.send_json(
                        {
                            "op": "error",
                            "error": "unknown_entity",
                            "message": "Unknown entity",
                            "entity": entity,
                        }
                    )
                elif len(subscriber.entities) >= MAX_SUBSCRIPTIONS and entity not in subscriber.entities:
                    await websocket.send_json(
                        {
                            "op": "error",
                            "error": "too_many_subscriptions",
                            "message": "Too many subscriptions",
                        }
                    )
                else:
                    subscriber.entities.add(entity)
                    await websocket.send_json({"op": "subscribed", "entity": entity})
            elif op == "unsubscribe" and isinstance(entity, str):
                subscriber.entities.discard(entity)
                await websocket.send_json({"op": "unsubscribed", "entity": entity})
            else:
                await websocket.send_json({"op": "error", "error": "unknown_op", "message": "Unknown op"})
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        expiry.cancel()
        hub.remove(subscriber)
