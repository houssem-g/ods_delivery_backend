"""getAdminOrderRoute: the road route of a course on the admin's sheets (owner 20/10/2026), admin only,
read only. OSRM is a MockTransport: nothing leaves the machine."""

import httpx
import pytest
from sqlalchemy import func, select

from app.api.functions import getRoutePreview as preview
from app.config import settings
from app.db import SessionLocal
from app.integrations import osrm
from app.models import Order, OrderOffer
from tests.factories import auth
from tests.order_helpers import OrderWorld


@pytest.fixture
async def world(factory):
    preview._cache.clear()
    return await OrderWorld(factory).setup()


def leg(seconds: float, metres: float, a: list[float], b: list[float]) -> dict:
    return {"duration": seconds, "distance": metres, "steps": [{"geometry": {"coordinates": [a, b]}}]}


@pytest.fixture
def osrm_ok(monkeypatch):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        n = request.url.path.rsplit("/", 1)[-1].count(";")
        legs = [
            leg(300, 1200, [10.60, 35.82], [10.601, 35.821]),
            leg(420, 3100, [10.61, 35.83], [10.62, 35.84]),
        ]
        return httpx.Response(200, json={"routes": [{"geometry": {"coordinates": []}, "legs": legs[-n:]}]})

    monkeypatch.setattr(settings, "OSRM_URL", "http://osrm.test")
    monkeypatch.setattr(osrm, "transport", httpx.MockTransport(handler))
    return calls


URL = "/api/functions/getAdminOrderRoute"


async def ask(client, user, order_id):
    return await client.post(URL, json={"order_id": str(order_id)}, headers=auth(user))


async def test_admin_gets_the_road_route_of_a_course_through_every_shop(client, world, osrm_ok):
    order = await world.order(status="delivered", courier=world.courier, stops=2)
    await world.offer(order, status="accepted")
    async with SessionLocal() as s:
        before = await s.scalar(select(func.count()).select_from(OrderOffer))
    r = await ask(client, world.admin, order.id)
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True and body["source"] == "osrm"
    assert [s["name"] for s in body["stops"]] == ["Monoprix", "Shop 2"]
    assert body["customer"]["address"] == "Rue de la plage"
    assert len(body["legs"]) == 2 and body["legs"][1]["distance_km"] == 3.1
    assert body["legs"][1]["path"][0] == [35.83, 10.61]
    assert body["distance_km"] == 4.3 and body["vehicle"] == world.courier.vehicle
    assert body["offer"]["eta_announced"] == 20 and body["courier_position"] is None
    async with SessionLocal() as s:  # read only
        assert await s.scalar(select(func.count()).select_from(OrderOffer)) == before
        assert (await s.get(Order, order.id)).status == "delivered"


async def test_without_a_router_the_straight_lines_are_given(client, world, monkeypatch):
    monkeypatch.setattr(settings, "OSRM_URL", "")
    order = await world.order()
    body = (await ask(client, world.admin, order.id)).json()
    assert body["success"] is True and body["source"] == "straight" and len(body["legs"]) == 1
    assert body["offer"] is None and body["vehicle"] == "scooter"


async def test_no_position_no_route(client, world, osrm_ok):
    order = await world.order(delivery=None)
    body = (await ask(client, world.admin, order.id)).json()
    assert body == {"success": False, "reason": "no_location"}
    assert osrm_ok == []


async def test_admin_only(client, world, osrm_ok):
    order = await world.order(courier=world.courier, status="accepted")
    for who in (world.customer, world.courier_user):
        r = await ask(client, who, order.id)
        assert r.status_code == 403
    anonymous = await client.post(URL, json={"order_id": str(order.id)})
    assert anonymous.status_code in (401, 403)
    assert (await ask(client, world.admin, "00000000-0000-0000-0000-000000000000")).status_code == 404
    assert (await ask(client, world.admin, "")).status_code == 400
    assert osrm_ok == []
