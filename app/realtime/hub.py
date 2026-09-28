"""In-process fan-out of entity events to WebSocket subscribers.

Events arrive from the Postgres listener in commit order and are processed one at a
time by a single worker. For each event, every distinct subscribed user gets the row
read through the compat registry *as that user* (read policy + field guards), so a
user who may not read the row receives nothing.
"""

import asyncio
import contextlib
import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from app.compat.dates import legacy_datetime
from app.compat.query import get_document
from app.compat.registry import get_entity
from app.db import SessionLocal
from app.security.deps import CurrentUser

log = logging.getLogger("odsd.realtime")
SEND_TIMEOUT_SECONDS = 5


class Socket(Protocol):
    async def send_json(self, data: Any) -> None: ...

    async def close(self, code: int = 1000, reason: str | None = None) -> None: ...


@dataclass(eq=False)
class Subscriber:
    socket: Socket
    user: CurrentUser
    entities: set[str] = field(default_factory=set)

    async def send(self, message: dict[str, Any]) -> bool:
        try:
            await asyncio.wait_for(self.socket.send_json(message), SEND_TIMEOUT_SECONDS)
        except Exception:
            return False
        return True


class Hub:
    def __init__(self) -> None:
        self.subscribers: set[Subscriber] = set()
        # Created by start(): a queue belongs to the event loop that runs the worker.
        self._queue: asyncio.Queue[dict[str, Any]] | None = None
        self._worker: asyncio.Task[None] | None = None

    def add(self, socket: Socket, user: CurrentUser) -> Subscriber:
        subscriber = Subscriber(socket=socket, user=user)
        self.subscribers.add(subscriber)
        return subscriber

    def remove(self, subscriber: Subscriber) -> None:
        self.subscribers.discard(subscriber)

    def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._queue = asyncio.Queue(maxsize=10_000)
            self._worker = asyncio.create_task(self._run(self._queue), name="realtime-hub")

    async def stop(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
            self._worker = None
        self._queue = None

    def publish(self, event: dict[str, Any]) -> None:
        """Called by the listener for each NOTIFY payload."""
        if self._queue is None:
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            log.error("realtime queue full: dropping %s", event)

    async def resync_all(self) -> None:
        """Events may have been missed (listener reconnected): clients refetch."""
        for subscriber in list(self.subscribers):
            await subscriber.send({"type": "resync"})

    async def _run(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        while True:
            event = await queue.get()
            try:
                await self.deliver(event)
            except Exception:
                log.exception("realtime delivery failed for %s", event)

    async def deliver(self, event: dict[str, Any]) -> None:
        name, type_, doc_id = event.get("entity"), event.get("type"), event.get("id")
        entity = get_entity(str(name))
        if entity is None or type_ not in ("create", "update", "delete") or not doc_id:
            return
        targets = [s for s in self.subscribers if name in s.entities]
        if not targets:
            return
        stamp = legacy_datetime(datetime.now(UTC))
        if type_ == "delete":
            audience = event.get("audience")
            allowed = None if audience is None else {str(a) for a in audience}
            for subscriber in targets:
                if allowed is None or subscriber.user.is_admin or str(subscriber.user.id) in allowed:
                    await self._send(
                        subscriber, {"entity": name, "type": type_, "id": doc_id, "timestamp": stamp}
                    )
            return
        by_user: dict[uuid.UUID, list[Subscriber]] = defaultdict(list)
        for subscriber in targets:
            by_user[subscriber.user.id].append(subscriber)
        async with SessionLocal() as session:
            for group in by_user.values():
                data = await get_document(session, entity, group[0].user, str(doc_id))
                if data is None:
                    continue
                for subscriber in group:
                    await self._send(
                        subscriber,
                        {"entity": name, "type": type_, "id": doc_id, "data": data, "timestamp": stamp},
                    )

    async def _send(self, subscriber: Subscriber, message: dict[str, Any]) -> None:
        if not await subscriber.send(message):
            self.remove(subscriber)


hub = Hub()
