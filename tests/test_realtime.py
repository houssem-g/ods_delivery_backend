"""Realtime: events published on commit, the LISTEN loop, the hub's per-user read check, the socket.

The end-to-end tests run the app with Starlette's TestClient (lifespan included), whose
event loop lives in another thread: every database call there goes through its portal.
"""

import asyncio
import functools
import time
from datetime import UTC, datetime

import pytest
from jose import jwt
from sqlalchemy import text
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.config import settings
from app.db import SessionLocal, engine
from app.main import app
from app.realtime import events
from app.realtime.hub import Hub
from app.realtime.listener import APPLICATION_NAME, PgListener
from app.security.deps import CurrentUser
from tests.factories import auth, token_for


@pytest.fixture
def live_app(factory):
    """TestClient with the lifespan running; pooled connections of the pytest loop are set aside."""
    engine.sync_engine.dispose(close=False)
    with TestClient(app) as client:
        client.portal.call(asyncio.wait_for, app.state.listener.connected.wait(), 10)
        client.run = lambda fn, *args, **kwargs: client.portal.call(functools.partial(fn, *args, **kwargs))
        yield client
    engine.sync_engine.dispose(close=False)


def subscribe(ws, *entities):
    for entity in entities:
        ws.send_json({"op": "subscribe", "entity": entity})
        assert ws.receive_json() == {"op": "subscribed", "entity": entity}


def test_events_follow_the_read_policy(live_app, factory):
    run = live_app.run
    admin = run(factory.user, role="admin")
    alice = run(factory.user, email="alice@example.test")
    bob = run(factory.user, email="bob@example.test")

    with (
        live_app.websocket_connect(f"/api/ws?token={token_for(alice)}") as ws_alice,
        live_app.websocket_connect(f"/api/ws?token={token_for(bob)}") as ws_bob,
    ):
        subscribe(ws_alice, "UserProfile", "AppSettings")
        subscribe(ws_bob, "UserProfile", "AppSettings")

        patched = live_app.patch(
            f"/api/entities/UserProfile/{alice.id}", json={"language": "fr"}, headers=auth(alice)
        )
        assert patched.status_code == 200
        event = ws_alice.receive_json()
        assert event["entity"] == "UserProfile" and event["type"] == "update" and event["id"] == str(alice.id)
        assert event["data"]["language"] == "fr" and event["data"]["user_id"] == alice.email
        assert event["timestamp"]

        # Events are delivered in order: bob's first event must be the settings one, not alice's profile.
        live_app.post(
            "/api/entities/AppSettings", json={"key": "main", "support_phone": "1"}, headers=auth(admin)
        )
        assert ws_bob.receive_json()["entity"] == "AppSettings"
        assert ws_alice.receive_json()["entity"] == "AppSettings"

        # A delete reaches only its audience (the profile owner and the admin who deleted it).
        live_app.delete(f"/api/entities/UserProfile/{alice.id}", headers=auth(admin))
        live_app.patch("/api/entities/AppSettings/main", json={"support_phone": "2"}, headers=auth(admin))
        deleted = ws_alice.receive_json()
        assert deleted == {
            "entity": "UserProfile",
            "type": "delete",
            "id": str(alice.id),
            "timestamp": deleted["timestamp"],
        }
        assert ws_alice.receive_json()["type"] == "update"
        bob_event = ws_bob.receive_json()
        assert (bob_event["entity"], bob_event["type"], bob_event["id"]) == ("AppSettings", "update", "main")
        assert bob_event["data"]["support_phone"] == "2"


def test_protocol_messages(live_app, factory):
    user = live_app.run(factory.user)
    with live_app.websocket_connect(f"/api/ws?token={token_for(user)}") as ws:
        ws.send_json({"op": "ping"})
        assert ws.receive_json() == {"op": "pong"}
        ws.send_json({"op": "subscribe", "entity": "Nope"})
        assert ws.receive_json()["error"] == "unknown_entity"
        ws.send_text("{not json")
        assert ws.receive_json()["error"] == "invalid_json"
        ws.send_json({"op": "dance"})
        assert ws.receive_json()["error"] == "unknown_op"
        subscribe(ws, "AppSettings")
        ws.send_json({"op": "unsubscribe", "entity": "AppSettings"})
        assert ws.receive_json() == {"op": "unsubscribed", "entity": "AppSettings"}


@pytest.mark.parametrize("token", ["", "garbage"])
def test_bad_token_is_closed_with_4401_after_accept(live_app, token):
    with (
        live_app.websocket_connect(f"/api/ws?token={token}") as ws,
        pytest.raises(WebSocketDisconnect) as closed,
    ):
        ws.receive_json()
    assert closed.value.code == 4401


def test_token_of_a_disabled_user_and_expiry(live_app, factory):
    disabled = live_app.run(factory.user, disabled_at=datetime.now(UTC))
    with (
        live_app.websocket_connect(f"/api/ws?token={token_for(disabled)}") as ws,
        pytest.raises(WebSocketDisconnect) as closed,
    ):
        ws.receive_json()
    assert closed.value.code == 4401

    user = live_app.run(factory.user)
    short = jwt.encode(
        {"sub": str(user.id), "role": "customer", "type": "access", "exp": int(time.time()) + 1},
        settings.JWT_SECRET,
        algorithm=settings.JWT_ALGORITHM,
    )
    with (
        live_app.websocket_connect(f"/api/ws?token={short}") as ws,
        pytest.raises(WebSocketDisconnect) as expired,
    ):
        ws.receive_json()
    assert expired.value.code == 4401


def test_listener_reconnects_and_asks_clients_to_resync(live_app, factory):
    user = live_app.run(factory.user)
    with live_app.websocket_connect(f"/api/ws?token={token_for(user)}") as ws:
        subscribe(ws, "AppSettings")

        async def kill_listener() -> None:
            async with SessionLocal() as s:
                await s.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name = :n"
                    ),
                    {"n": APPLICATION_NAME},
                )

        live_app.run(kill_listener)
        assert ws.receive_json() == {"type": "resync"}


# --- units --------------------------------------------------------------------------------------


class FakeSocket:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[dict] = []
        self.fail = fail

    async def send_json(self, data):
        if self.fail:
            raise RuntimeError("gone")
        self.sent.append(data)

    async def close(self, code=1000, reason=None):
        pass


def _user(role="customer") -> CurrentUser:
    import uuid

    return CurrentUser(id=uuid.uuid4(), email="u@example.test", role=role, full_name="")


async def test_hub_delete_audience_and_dead_sockets():
    hub = Hub()
    member, stranger, admin = _user(), _user(), _user("admin")
    sockets = {u.id: FakeSocket() for u in (member, stranger, admin)}
    for u in (member, stranger, admin):
        hub.add(sockets[u.id], u).entities.add("AppSettings")
    await hub.deliver({"entity": "AppSettings", "type": "delete", "id": "main", "audience": [str(member.id)]})
    assert [len(sockets[u.id].sent) for u in (member, stranger, admin)] == [1, 0, 1]

    broken = hub.add(FakeSocket(fail=True), _user())
    broken.entities.add("AppSettings")
    await hub.deliver({"entity": "AppSettings", "type": "delete", "id": "main"})
    assert broken not in hub.subscribers

    await hub.deliver({"entity": "Unknown", "type": "update", "id": "x"})
    await hub.deliver({"entity": "AppSettings", "type": "bogus", "id": "x"})
    hub.publish({"entity": "AppSettings"})  # not started: ignored


async def test_hub_worker_processes_published_events():
    hub = Hub()
    member = _user()
    socket = FakeSocket()
    hub.add(socket, member).entities.add("AppSettings")
    hub.start()
    hub.publish({"entity": "AppSettings", "type": "delete", "id": "main"})
    for _ in range(50):
        if socket.sent:
            break
        await asyncio.sleep(0.02)
    await hub.stop()
    assert socket.sent[0]["type"] == "delete"


async def test_emit_is_dropped_on_rollback_and_deduplicated(session):
    await session.execute(text("SELECT 1"))
    events.emit(session, "AppSettings", "update", "main")
    events.emit(session, "AppSettings", "update", "main")
    assert len(events.pending_events(session)) == 1
    await session.rollback()
    assert events.pending_events(session) == []


async def test_notify_is_sent_only_on_commit():
    hub = Hub()
    received: list[dict] = []
    hub.publish = received.append  # type: ignore[method-assign]
    listener = PgListener(hub)
    listener.start()
    await asyncio.wait_for(listener.connected.wait(), 10)
    async with SessionLocal() as s:
        events.emit(s, "AppSettings", "update", "rolled-back")
        await s.rollback()
    async with SessionLocal() as s:
        events.emit(s, "AppSettings", "create", "committed", audience=["u1"])
        await s.commit()
    for _ in range(100):
        if received:
            break
        await asyncio.sleep(0.02)
    listener._on_notify(None, 0, "c", "not json")
    await listener.stop()
    assert received == [{"entity": "AppSettings", "type": "create", "id": "committed", "audience": ["u1"]}]
