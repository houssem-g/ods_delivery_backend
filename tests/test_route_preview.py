"""getRoutePreview: the road route courier → shop → customer of an order on the Commandes map
(owner, 10/10/2026). OSRM is a MockTransport: nothing leaves the machine."""

import httpx
import pytest

from app.api.functions import getRoutePreview as preview
from app.config import settings
from app.integrations import osrm
from tests.factories import auth
from tests.order_helpers import OrderWorld

SOUSSE_ME = (35.8256, 10.6084)
PARIS = (48.85, 2.35)


@pytest.fixture
async def world(factory):
    preview._cache.clear()
    return await OrderWorld(factory).setup()


def leg(seconds: float, metres: float, a: list[float], b: list[float]) -> dict:
    return {
        "duration": seconds,
        "distance": metres,
        "steps": [
            {"geometry": {"coordinates": [a, b]}},
            {"geometry": {"coordinates": [b, [b[0] + 0.001, b[1]]]}},
        ],
    }


@pytest.fixture
def osrm_ok(monkeypatch):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        n = request.url.path.rsplit("/", 1)[-1].count(";")
        legs = [
            leg(600, 5200, [10.60, 35.82], [10.61, 35.83]),
            leg(420, 3100, [10.61, 35.83], [10.62, 35.84]),
        ]
        return httpx.Response(200, json={"routes": [{"geometry": {"coordinates": []}, "legs": legs[-n:]}]})

    monkeypatch.setattr(settings, "OSRM_URL", "http://osrm.test")
    monkeypatch.setattr(osrm, "transport", httpx.MockTransport(handler))
    return calls


async def ask(client, user, order_id, at=None):
    body = {"order_id": str(order_id)}
    if at:
        body |= {"from_lat": at[0], "from_lng": at[1]}
    return await client.post("/api/functions/getRoutePreview", json=body, headers=auth(user))


async def test_courier_gets_the_road_route_and_it_is_cached(client, world, osrm_ok):
    order = await world.order()
    r = await ask(client, world.courier_user, order.id, SOUSSE_ME)
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True and body["source"] == "osrm"
    assert body["to_shop"]["distance_km"] == 5.2 and body["to_shop"]["duration_min"] == 10
    assert body["delivery"]["distance_km"] == 3.1 and body["delivery"]["duration_min"] == 7
    assert body["to_shop"]["path"][0] == [35.82, 10.6] and len(body["delivery"]["path"]) == 3
    assert body["distance_km"] == 8.3 and body["drive_minutes"] == 17 and body["eta_minutes"] == 32
    # a few metres further: same answer from memory, no second OSRM call
    again = await ask(client, world.courier_user, order.id, (SOUSSE_ME[0] + 0.0001, SOUSSE_ME[1]))
    assert again.json() == body and len(osrm_ok) == 1


async def test_abroad_position_is_ignored(client, world, osrm_ok):
    order = await world.order()
    body = (await ask(client, world.courier_user, order.id, PARIS)).json()
    assert body["success"] is True and body["to_shop"] is None and body["delivery"]["distance_km"] == 3.1
    assert osrm_ok[0].count(";") == 1  # shop → customer only


async def test_osrm_down_keeps_the_estimate(client, world, monkeypatch):
    monkeypatch.setattr(settings, "OSRM_URL", "http://osrm.test")
    monkeypatch.setattr(
        osrm, "transport", httpx.MockTransport(lambda request: httpx.Response(503, text="busy"))
    )
    order = await world.order()
    body = (await ask(client, world.courier_user, order.id, SOUSSE_ME)).json()
    assert body == {"success": False, "reason": "unavailable"}


async def test_only_readers_of_the_order(client, world, factory, osrm_ok):
    order = await world.order()
    stranger = await factory.user(email="stranger@example.test")
    assert (await ask(client, stranger, order.id)).status_code == 404
    assert (
        await client.post("/api/functions/getRoutePreview", json={}, headers=auth(stranger))
    ).status_code == 400
    no_shop = await world.order(shop=None)
    assert (await ask(client, world.courier_user, no_shop.id)).json() == {
        "success": False,
        "reason": "no_location",
    }
