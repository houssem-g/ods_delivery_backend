"""getNetworkPulse (live supply, anonymous allowed) and getDemandPulse (couriers)."""

from datetime import timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app.db import SessionLocal
from app.models import GeocodeCache, OrderOffer
from app.services import pulse
from app.services.geo import haversine_km
from tests.factories import auth
from tests.order_helpers import SOUSSE_HOME, SOUSSE_SHOP, TUNIS, OrderWorld, now

TUNIS_TZ = ZoneInfo("Africa/Tunis")


@pytest.fixture
async def world(factory):
    return await OrderWorld(factory).setup()


async def call(client, user, name: str, payload: dict[str, Any] | None = None, **headers: str):
    head = {**(auth(user) if user else {}), **headers}
    return await client.post(f"/api/functions/{name}", json=payload or {}, headers=head)


async def courier_at(world, n: int, at: tuple[float, float], **fields: Any):
    user = await world.factory.user(email=f"c{n}-{at[0]}@example.test", profile=False)
    return await world.make_courier(user, at=at, display_name=f"C{n}", **fields)


async def offer_after(order, courier, minutes: float) -> None:
    async with SessionLocal() as s:
        s.add(
            OrderOffer(
                order_id=order.id,
                courier_id=courier.id,
                proposed_fee=Decimal("5"),
                status="pending",
                created_at=order.created_at + timedelta(minutes=minutes),
            )
        )
        await s.commit()


# ─────────────────────────── getNetworkPulse ───────────────────────────


async def test_anonymous_pulse_without_a_point(client, world):
    await courier_at(world, 1, TUNIS, service_city="Tunis")
    await courier_at(world, 2, SOUSSE_SHOP, verification="pending")
    await courier_at(world, 3, SOUSSE_SHOP, is_online=False)
    r = await call(client, None, "getNetworkPulse")
    assert r.status_code == 200, r.text
    assert r.json() == {
        "success": True,
        "city": "Sousse",
        "online_city": 2,  # world.courier (Sousse) + the Tunis one; pending / offline never count
        "online_near": None,
        "radius_km": 3.0,
        "first_offer_minutes": None,
    }


async def test_anonymous_pulse_around_a_point(client, world):
    await courier_at(world, 1, TUNIS, service_city="Tunis")
    await courier_at(world, 2, (35.86, 10.60), service_city="Hammam Sousse")  # ~4 km
    await courier_at(world, 3, (35.8301, 10.6201), service_city="Hammam Sousse")
    body = (
        await call(client, None, "getNetworkPulse", {"lat": SOUSSE_HOME[0], "lng": SOUSSE_HOME[1]})
    ).json()
    assert body["city"] == "Hammam Sousse"
    assert body["online_city"] == 3 and body["online_near"] == 2  # default 3 km
    assert "courier_dots" not in body and "fee_range" not in body
    wide = (
        await call(
            client, None, "getNetworkPulse", {"lat": SOUSSE_HOME[0], "lng": SOUSSE_HOME[1], "radius_km": 99}
        )
    ).json()
    assert wide["radius_km"] == 10 and wide["online_near"] == 3
    # a point abroad is not used
    abroad = (await call(client, None, "getNetworkPulse", {"lat": 48.85, "lng": 2.35})).json()
    assert abroad["online_near"] is None and abroad["online_city"] == 4


async def test_city_falls_back_to_the_geocode_cache(client, world):
    async with SessionLocal() as s:
        s.add(
            GeocodeCache(
                key="k",
                provider="nominatim",
                found=True,
                lat=36.40,
                lng=10.14,
                result={"city": "", "governorate": "Zaghouan"},
                expires_at=now() + timedelta(days=30),
            )
        )
        await s.commit()
    body = (await call(client, None, "getNetworkPulse", {"lat": 36.41, "lng": 10.14})).json()
    assert body["city"] == "Zaghouan" and body["online_city"] == 0
    nowhere = (await call(client, None, "getNetworkPulse", {"lat": 33.0, "lng": 9.0})).json()
    assert nowhere["city"] == "Sousse"


async def test_first_offer_minutes_is_a_median_of_at_least_five(client, world):
    for minutes in (2, 4, 6, 30):
        order = await world.order()
        await offer_after(order, world.courier, minutes)
    await world.order()  # no offer: not a sample
    qa = await world.order(items="QA TEST pain")
    await offer_after(qa, world.courier, 1)  # QA orders never count
    old = await world.order(created_at=now() - timedelta(days=15))
    await offer_after(old, world.courier, 1)  # older than 14 days
    point = {"lat": SOUSSE_HOME[0], "lng": SOUSSE_HOME[1]}
    assert (await call(client, None, "getNetworkPulse", point)).json()["first_offer_minutes"] is None
    order = await world.order()
    await offer_after(order, world.courier, 5)
    await offer_after(order, await courier_at(world, 9, SOUSSE_SHOP), 50)  # only the first offer
    assert (await call(client, None, "getNetworkPulse", point)).json()["first_offer_minutes"] == 5.0
    far = (await call(client, None, "getNetworkPulse", {"lat": TUNIS[0], "lng": TUNIS[1]})).json()
    assert far["first_offer_minutes"] is None


def test_snap_is_a_coarse_grid():
    assert pulse.snap(35.8256, 10.6084) == (35.826, 10.61)
    assert pulse.snap(35.8241, 10.6081) == (35.826, 10.61)  # same 0.004° cell
    lat, lng = pulse.snap(35.8299, 10.6199)
    assert (lat, lng) != (35.8299, 10.6199)


async def test_signed_in_pulse_has_coarse_dots_and_the_shop_figures(client, world):
    # world.courier sits on the shop; two more in one cell near home, one 2 km away
    await courier_at(world, 1, (35.8301, 10.6201), price_per_km=Decimal("0.800"), min_fee=Decimal("3"))
    await courier_at(world, 2, (35.8302, 10.6202), price_per_km=Decimal("2.000"))
    await courier_at(world, 3, (35.8480, 10.6200))
    payload = {
        "lat": SOUSSE_HOME[0],
        "lng": SOUSSE_HOME[1],
        "shop_lat": SOUSSE_SHOP[0],
        "shop_lng": SOUSSE_SHOP[1],
    }
    body = (await call(client, world.customer, "getNetworkPulse", payload)).json()
    dots = body["courier_dots"]
    assert len(dots) == 3  # four couriers, two in the same cell
    assert {"lat": 35.83, "lng": 10.622} in dots
    for dot in dots:
        assert (dot["lat"], dot["lng"]) == pulse.snap(dot["lat"], dot["lng"])  # grid centres only
        assert (dot["lat"], dot["lng"]) not in {(35.8301, 10.6201), (35.8302, 10.6202), SOUSSE_SHOP}
    assert body["couriers_near_shop"] == 1  # within 1 km of the shop: world.courier only
    # QA B12: those the order would be sent to (their notification radius covers the shop)
    assert body["couriers_for_shop"] == 4
    # the app's suggested price of each courier within 3 km of the shop: his tariff × (him → shop
    # + shop → home ≈ 1.16 km), his minimum, 1 DT at least, 0.5 steps (QA B8: as computeOfferQuote)
    ride = haversine_km(*SOUSSE_SHOP, *SOUSSE_HOME)
    near_home = haversine_km(35.8301, 10.6201, *SOUSSE_SHOP) + ride
    near_home_2 = haversine_km(35.8302, 10.6202, *SOUSSE_SHOP) + ride
    fees = [
        round(max(1, ride * 1) * 2) / 2,  # world.courier, on the shop, 1 DT/km
        round(max(3, near_home * 0.8) * 2) / 2,
        round(max(1, near_home_2 * 2) * 2) / 2,
    ]
    assert body["fee_range"] == {"min": min(fees), "max": max(fees)} == {"min": 1.0, "max": 4.5}
    no_point = (
        await call(
            client,
            world.customer,
            "getNetworkPulse",
            {"shop_lat": SOUSSE_SHOP[0], "shop_lng": SOUSSE_SHOP[1]},
        )
    ).json()
    assert no_point["courier_dots"] == [] and no_point["fee_range"] is None


async def test_fee_range_is_null_without_couriers(client, world):
    far_shop = {"lat": 33.88, "lng": 10.09, "shop_lat": 33.881, "shop_lng": 10.1}
    body = (await call(client, world.customer, "getNetworkPulse", far_shop)).json()
    assert body["couriers_near_shop"] == 0 and body["fee_range"] is None and body["courier_dots"] == []
    assert body["couriers_for_shop"] == 0


async def test_pulse_rate_limits(client, world):
    for _ in range(30):
        assert (await call(client, None, "getNetworkPulse")).status_code == 200
    refused = await call(client, None, "getNetworkPulse")
    assert refused.status_code == 429 and refused.json()["error"] == "too_many_pulse_requests"
    # signed-in callers have their own budget
    assert (await call(client, world.customer, "getNetworkPulse")).status_code == 200


# ─────────────────────────── getDemandPulse ───────────────────────────


async def test_demand_pulse_access(client, world):
    r = await call(client, world.customer, "getDemandPulse", {"lat": 35.83, "lng": 10.62})
    assert r.status_code == 403 and r.json()["error"] == "courier_profile_missing"
    r = await call(client, world.courier_user, "getDemandPulse", {"lat": 48.8, "lng": 2.3})
    assert r.status_code == 400 and r.json()["error"] == "invalid_location"
    assert (await client.post("/api/functions/getDemandPulse", json={})).status_code == 401


async def test_demand_zones_and_trend(client, world):
    home = SOUSSE_HOME
    for _ in range(3):
        await world.order(delivery=home, delivery_city="Khezama")
    await world.order(
        delivery=(35.8350, 10.6250), delivery_city="Khezama", status="cancelled",
        created_at=now() - timedelta(hours=5),
    )  # fmt: skip
    open_old = await world.order(delivery=(35.8350, 10.6250), delivery_city="Khezama")
    async with SessionLocal() as s:  # an open order from 5 h ago still counts
        from app.models import Order

        row = await s.get(Order, open_old.id)
        row.created_at = now() - timedelta(hours=5)
        await s.commit()
    await world.order(delivery=(35.8260, 10.6300), delivery_city=None)  # shop city: none → governorate
    for _ in range(2):
        await world.order(
            delivery=home, delivery_city="Khezama", status="delivered", courier=world.courier,
            created_at=now() - timedelta(days=7, minutes=30),
        )  # fmt: skip
    await world.order(delivery=home, delivery_city="Sahloul", status="cancelled")
    await world.order(delivery=home, delivery_city="Sahloul", items="PW-123 test")  # QA
    await world.order(delivery=TUNIS, delivery_city="Lac")  # out of the radius
    body = (
        await call(
            client, world.courier_user, "getDemandPulse", {"lat": home[0], "lng": home[1], "radius_km": 5}
        )
    ).json()
    assert body["success"] is True and body["radius_km"] == 5
    zones = body["zones"]
    assert [z["name"] for z in zones] == ["Khezama", "Sahloul", "Sousse"]
    khezama = zones[0]
    assert khezama["count"] == 4 and khezama["trend_pct"] == 100  # 4 now vs 2 a week ago
    assert khezama["lat"] == pytest.approx((3 * home[0] + 35.8350) / 4, abs=1e-4)
    assert zones[1] == {"name": "Sahloul", "lat": home[0], "lng": home[1], "count": 1, "trend_pct": None}
    assert body["orders_per_day"] == round(9 / 14, 1)


async def test_demand_hourly_average_and_peak(client, world):
    slot = now().astimezone(TUNIS_TZ).replace(minute=10, second=0, microsecond=0)
    for weeks, hours, n in ((1, 0, 2), (2, 1, 2), (3, 1, 2), (4, 5, 4), (5, 0, 8)):
        for _ in range(n):
            await world.order(created_at=slot - timedelta(days=7 * weeks) + timedelta(hours=hours))
    body = (
        await call(
            client, world.courier_user, "getDemandPulse", {"lat": SOUSSE_HOME[0], "lng": SOUSSE_HOME[1]}
        )
    ).json()
    assert body["radius_km"] == 10  # the courier's notification radius
    hours = [(slot + timedelta(hours=i)).hour for i in range(6)]
    assert [h["hour"] for h in body["hourly"]] == hours
    assert [h["avg"] for h in body["hourly"]] == [0.5, 1.0, 0.0, 0.0, 0.0, 1.0]  # week 5 is too old
    assert body["peak"] == {"from": hours[0], "to": (hours[0] + 2) % 24}
    assert body["zones"] == []


async def test_demand_peak_is_null_without_history(client, world):
    body = (await call(client, world.courier_user, "getDemandPulse", {"lat": 35.0, "lng": 9.5})).json()
    assert body["peak"] is None and body["orders_per_day"] == 0 and body["zones"] == []
    assert all(h["avg"] == 0 for h in body["hourly"])


async def test_demand_pulse_rate_limit(client, world, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "RATE_LIMIT_DEMAND_PULSE", "2/minute")
    payload = {"lat": SOUSSE_HOME[0], "lng": SOUSSE_HOME[1]}
    for _ in range(2):
        assert (await call(client, world.courier_user, "getDemandPulse", payload)).status_code == 200
    refused = await call(client, world.courier_user, "getDemandPulse", payload)
    assert refused.status_code == 429 and refused.json()["error"] == "too_many_pulse_requests"


async def test_courier_dots_are_capped(client, world, monkeypatch):
    monkeypatch.setattr(pulse, "DOTS_MAX", 2)
    for n in range(3):
        await courier_at(world, n, (35.8300 + n * 0.005, 10.6200))
    body = (await call(client, world.customer, "getNetworkPulse", {"lat": 35.83, "lng": 10.62})).json()
    assert len(body["courier_dots"]) == 2
