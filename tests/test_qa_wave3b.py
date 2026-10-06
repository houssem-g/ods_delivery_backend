"""QA campaign 06/10 — wave 3, courier side (team b).

B18 every date the API returns carries its zone (« Z »): a phone set to Tunis (UTC+1) read the
naive « 2026-10-06T13:05:57 » as local time, the « offer accepted » notification looked one hour
old and the courier was never alerted."""

import json
from datetime import UTC, datetime
from typing import Any

import httpx

from app.compat.dates import legacy_datetime, parse_legacy_datetime
from tests.factories import auth
from tests.order_helpers import OrderWorld


async def fn(client, user, name: str, body: dict[str, Any]) -> httpx.Response:
    return await client.post(f"/api/functions/{name}", json=body, headers=auth(user))


def _zoned(value: str) -> datetime:
    assert isinstance(value, str) and value.endswith("Z"), value
    return datetime.fromisoformat(value)


# ─────────────────────────── B18 ───────────────────────────


def test_dates_are_written_in_utc_with_their_zone():
    assert legacy_datetime(datetime(2026, 10, 6, 13, 5, 57)) == "2026-10-06T13:05:57.000000Z"
    tunis = datetime(2026, 10, 6, 14, 5, 57, tzinfo=UTC)
    assert legacy_datetime(tunis.astimezone()) == "2026-10-06T14:05:57.000000Z"
    # what we write, we read back (filters sent by the front use the same strings)
    assert parse_legacy_datetime(legacy_datetime(tunis)) == tunis


async def test_offer_accepted_notification_has_a_zoned_fresh_date(client, factory):
    world = await OrderWorld(factory).setup()
    order = await world.order(status="offers_received")
    offer = await world.offer(order, fee="7")
    r = await fn(client, world.customer, "acceptOrderOffer", {"order_id": str(order.id), "offer_id": str(offer.id)})
    assert r.status_code == 200, r.text

    q = {"q": json.dumps({"user_id": world.courier_user.email, "type": "order_accepted", "is_read": False})}
    rows = (await client.get("/api/entities/Notification", params=q, headers=auth(world.courier_user))).json()
    assert len(rows) == 1
    note = rows[0]
    created = _zoned(note["created_date"])
    assert abs((datetime.now(UTC) - created).total_seconds()) < 60  # fresh, whatever the phone's zone
    _zoned(note["updated_date"])

    doc = (await client.get(f"/api/entities/Order/{order.id}", headers=auth(world.customer))).json()
    for key in ("created_date", "updated_date", "accepted_at"):
        _zoned(doc[key])
