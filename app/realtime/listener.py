"""LISTEN on the events channel over a dedicated asyncpg connection, reconnecting with backoff."""

import asyncio
import contextlib
import json
import logging

import asyncpg

from app.config import settings
from app.db import asyncpg_dsn
from app.realtime.hub import Hub

log = logging.getLogger("odsd.realtime")
MAX_BACKOFF_SECONDS = 30
APPLICATION_NAME = "ods-delivery-listener"


class PgListener:
    def __init__(self, hub: Hub, channel: str | None = None) -> None:
        self.hub = hub
        self.channel = channel or settings.REALTIME_CHANNEL
        self._task: asyncio.Task[None] | None = None
        self.connected = asyncio.Event()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="realtime-listener")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self.connected.clear()

    def _on_notify(self, _conn: object, _pid: int, _channel: str, payload: str) -> None:
        try:
            event = json.loads(payload)
        except ValueError:
            log.warning("ignoring malformed realtime payload")
            return
        if isinstance(event, dict):
            self.hub.publish(event)

    async def _run(self) -> None:
        backoff = 1.0
        first = True
        while True:
            conn: asyncpg.Connection | None = None
            try:
                conn = await asyncpg.connect(
                    asyncpg_dsn(settings), timeout=10, server_settings={"application_name": APPLICATION_NAME}
                )
                lost = asyncio.Event()
                conn.add_termination_listener(lambda _c, event=lost: event.set())
                await conn.add_listener(self.channel, self._on_notify)
                self.connected.set()
                backoff = 1.0
                if not first:
                    await self.hub.resync_all()
                first = False
                log.info("realtime listener connected (channel %s)", self.channel)
                await self._wait_until_lost(conn, lost)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("realtime listener error: %s", exc)
            finally:
                self.connected.clear()
                if conn is not None and not conn.is_closed():
                    with contextlib.suppress(Exception):
                        await conn.close(timeout=2)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)

    @staticmethod
    async def _wait_until_lost(conn: asyncpg.Connection, lost: asyncio.Event) -> None:
        """Returns when the connection dies. A periodic ping catches half-open TCP connections."""
        while not lost.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(lost.wait(), timeout=30)
            if not lost.is_set():
                await asyncio.wait_for(conn.execute("SELECT 1"), timeout=10)
