"""Courier earnings forecast (app/services/forecast.py): declarations, invited clients, the month
forecast, the simulation's behaviour, and the self-correction (week snapshots evaluated against
reality, shared factors re-learned, a courier's bias applied to his next forecasts)."""

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select, update

from app.db import SessionLocal
from app.models import AppSetting, Courier, EarningsForecast, ForecastFactor, Order, User
from app.services import forecast
from tests.factories import auth
from tests.order_helpers import OrderWorld, now


@pytest.fixture
async def world(factory):
    return await OrderWorld(factory).setup()


async def call(client, user, name: str, payload: dict[str, Any] | None = None):
    return await client.post(f"/api/functions/{name}", json=payload or {}, headers=auth(user))


async def courier_since(world, days: int) -> None:
    async with SessionLocal() as s:
        await s.execute(
            update(Courier)
            .where(Courier.id == world.courier.id)
            .values(created_at=now() - timedelta(days=days))
        )
        await s.commit()


async def invited(factory, world, name: str, days_ago: int = 30, **fields) -> User:
    at = now() - timedelta(days=days_ago)
    user = await factory.user(full_name=name, **fields)
    async with SessionLocal() as s:
        await s.execute(
            update(User)
            .where(User.id == user.id)
            .values(referred_by_courier_id=world.courier.id, referred_at=at, profile_created_at=at)
        )
        await s.commit()
    return user


async def delivered(world, customer: User, days_ago: float, fee: str = "6") -> Order:
    order = await world.order(customer, status="delivered", courier=world.courier, fee=fee)
    async with SessionLocal() as s:
        await s.execute(
            update(Order).where(Order.id == order.id).values(delivered_at=now() - timedelta(days=days_ago))
        )
        await s.commit()
    return order


async def month_of(world, seed: int = 7) -> dict[str, Any]:
    async with SessionLocal() as s:
        courier = await s.get(Courier, world.courier.id)
        return await forecast.month_forecast(s, courier, seed=seed)


async def test_no_forecast_without_any_data(client, world):
    r = await call(client, world.courier_user, "getMyEarningsForecast")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available"] is False and body["reason"] == "not_enough_data"
    assert (await call(client, world.customer, "getMyEarningsForecast")).json()[
        "error"
    ] == "courier_profile_missing"


async def test_declarations_alone_give_a_prudent_range(client, world):
    r = await call(
        client,
        world.courier_user,
        "saveMyCourierActivity",
        {"weekly_deliveries": 30, "active_days": 5, "regular_clients": 12},
    )
    assert r.status_code == 200 and r.json()["active_days"] == 5, r.text
    assert (await call(client, world.courier_user, "saveMyCourierActivity", {"active_days": 9})).json()[
        "error"
    ] == "invalid_active_days"
    body = (await call(client, world.courier_user, "getMyEarningsForecast")).json()
    assert body["available"] is True and body["status"] == "starting"
    assert Decimal(body["low"]) <= Decimal(body["mid"]) <= Decimal(body["high"])
    assert body["from_others"] > 0 and body["from_clients"] == 0
    assert body["uses_declarations"] is True
    # still in the launch: no commission taken
    assert body["commission_estimate"] == "0.000"
    # the declaration is discounted: never more than declared (30/week * ~4.3 weeks)
    assert body["expected_deliveries"] < 30 / 7 * body["days_left"] + 1


async def test_more_declared_activity_means_more_forecast(world):
    async with SessionLocal() as s:
        await s.execute(
            update(Courier)
            .where(Courier.id == world.courier.id)
            .values(declared_weekly_deliveries=10, declared_active_days=5)
        )
        await s.commit()
    small = await month_of(world)
    async with SessionLocal() as s:
        await s.execute(
            update(Courier).where(Courier.id == world.courier.id).values(declared_weekly_deliveries=40)
        )
        await s.commit()
    big = await month_of(world)
    assert Decimal(big["mid"]) > Decimal(small["mid"])


async def test_invited_clients_and_estimates(client, world, factory):
    ali = await invited(factory, world, "Ali Ben Salah")
    await invited(factory, world, "Sonia Trabelsi", declared_monthly_orders=8)
    stranger = await factory.user(full_name="Autre")
    await delivered(world, ali, 20)
    await delivered(world, ali, 5)

    listed = (await call(client, world.courier_user, "listMyInvitedClients")).json()["clients"]
    assert {c["name"] for c in listed} == {"Ali", "Sonia"}
    assert {c["name"]: c["delivered_orders"] for c in listed} == {"Ali": 2, "Sonia": 0}

    ok = await call(
        client,
        world.courier_user,
        "setInvitedClientEstimate",
        {"client_id": str(ali.id), "monthly_orders": 6},
    )
    assert ok.json() == {"success": True, "client_id": str(ali.id), "monthly_orders": 6}
    nope = await call(
        client,
        world.courier_user,
        "setInvitedClientEstimate",
        {"client_id": str(stranger.id), "monthly_orders": 6},
    )
    assert nope.status_code == 404
    bad = await call(
        client,
        world.courier_user,
        "setInvitedClientEstimate",
        {"client_id": str(ali.id), "monthly_orders": 99},
    )
    assert bad.json()["error"] == "invalid_monthly_orders"

    body = (await call(client, world.courier_user, "getMyEarningsForecast")).json()
    assert body["available"] is True and body["clients_counted"] == 2
    assert body["from_clients"] > 0 and body["deliveries_so_far"] >= 0
    assert any(t["kind"] == "invite" for t in body["tips"])


async def test_customer_answers_how_often(client, world):
    r = await call(client, world.customer, "saveMyOrderFrequency", {"monthly_orders": 4})
    assert r.json() == {"success": True, "monthly_orders": 4}
    async with SessionLocal() as s:
        assert (await s.get(User, world.customer.id)).declared_monthly_orders == 4
    # the profile reads the answer back (auth.me()): the question is not asked again
    me = await client.get("/api/auth/me", headers=auth(world.customer))
    assert me.json()["declared_monthly_orders"] == 4
    assert (await call(client, world.customer, "saveMyOrderFrequency", {"monthly_orders": -1})).json()[
        "error"
    ] == "invalid_monthly_orders"
    assert (await call(client, world.customer, "saveMyOrderFrequency", {"monthly_orders": None})).json()[
        "monthly_orders"
    ] is None


async def test_a_silent_client_fades_out(world, factory):
    regular = await invited(factory, world, "Régulier", days_ago=100)
    gone = await invited(factory, world, "Parti", days_ago=100)
    for d in (3, 10, 17, 24):
        await delivered(world, regular, d)
    for d in (95, 88, 81, 74):  # ordered weekly, then nothing for 74 days
        await delivered(world, gone, d)
    async with SessionLocal() as s:
        courier = await s.get(Courier, world.courier.id)
        inp = await forecast.gather(s, courier)
    alive = {c.name: c.p_alive for c in inp.clients}
    assert alive["Régulier"] == 1.0 and alive["Parti"] < 0.5


async def test_commission_is_taken_after_the_launch(world, factory, monkeypatch):
    monkeypatch.setattr(forecast, "LAUNCH_END_DATE", now() - timedelta(days=400))
    ali = await invited(factory, world, "Ali")
    for _ in range(25):
        await delivered(world, ali, 0)
    body = await month_of(world)
    assert body["deliveries_so_far"] == 25
    assert Decimal(body["commission_estimate"]) >= Decimal("1.250")  # (25 - 20) x 0.250 at least


async def test_simulation_is_reproducible_and_ordered(world):
    async with SessionLocal() as s:
        await s.execute(
            update(Courier)
            .where(Courier.id == world.courier.id)
            .values(declared_weekly_deliveries=20, declared_active_days=4)
        )
        await s.commit()
        courier = await s.get(Courier, world.courier.id)
        inp = await forecast.gather(s, courier)
    days = [date(2026, 11, 2) + timedelta(days=i) for i in range(7)]
    a = forecast.simulate(inp, days, seed=1, runs=500)
    b = forecast.simulate(inp, days, seed=1, runs=500)
    assert a == b
    assert 0 <= a["p25"] <= a["p50"] <= a["p75"]
    assert abs(sum(a["daily_expected"].values()) - a["expected_deliveries"]) < 0.01


async def test_self_correction_learns_from_reality(client, world, factory):
    """A forecast that was too optimistic lowers the shared bias and this courier's next forecast."""
    ali = await invited(factory, world, "Ali")
    await call(
        client, world.courier_user, "saveMyCourierActivity", {"weekly_deliveries": 40, "active_days": 6}
    )
    before = await month_of(world)

    today = forecast.tunis_today()
    async with SessionLocal() as s:
        for weeks_ago in (5, 4, 3, 2):
            start = today - timedelta(days=7 * weeks_ago)
            days = {(start + timedelta(days=i)).isoformat(): 5.0 for i in range(7)}
            s.add(
                EarningsForecast(
                    courier_id=world.courier.id,
                    kind="week",
                    as_of=start - timedelta(days=1),
                    period_start=start,
                    period_end=start + timedelta(days=6),
                    p25=Decimal("150"),
                    p50=Decimal("200"),
                    p75=Decimal("250"),
                    expected_deliveries=Decimal("35"),
                    method=forecast.METHOD,
                    details={"daily_expected": days},
                )
            )
        await s.commit()
    for weeks_ago in (5, 4, 3, 2):  # reality: 1 delivery of 6 DT per week
        await delivered(world, ali, 7 * weeks_ago - 1)

    async with SessionLocal() as s:
        result = await forecast.nightly(s)
        await s.commit()
    assert result["evaluated"] == 4 and result["snapshots"] == 1
    async with SessionLocal() as s:
        factors = {f.key: float(f.value) for f in (await s.execute(select(ForecastFactor))).scalars()}
        snaps = list(
            (
                await s.execute(select(EarningsForecast).where(EarningsForecast.evaluated_at.is_not(None)))
            ).scalars()
        )
    assert all(sn.actual_deliveries == 1 and sn.actual_fees == Decimal("6.000") for sn in snaps)
    assert factors["bias_global"] < 1.0
    assert factors["coverage_global"] == 0.0
    assert any(k.startswith("weekday_") and v < 1.0 for k, v in factors.items())

    after = await month_of(world)
    assert Decimal(after["mid"]) < Decimal(before["mid"])
    assert after["evaluations"] == 4
    assert after["status"] in ("learning", "calibrated")
    assert after["status"] == "learning"  # missed by far more than MAX_ERROR


async def test_rain_and_calendar_kinds(world, factory):
    async def fake_fetch(url):
        return {"daily": {"time": ["2027-02-10", "2027-02-11"], "precipitation_sum": [5.2, 0.0]}}

    async with SessionLocal() as s:
        assert (await forecast.refresh_weather(s, fetch=fake_fetch)) == {"days": 2}
        await s.commit()
        row = await s.get(AppSetting, forecast.WEATHER_KEY)
        assert row.value["daily"]["2027-02-10"] == 5.2
        rainy = await forecast._rainy_days(s)
    assert forecast.day_kinds(date(2027, 2, 10), rainy) == ["weekday_2", "ramadan", "rain"]
    assert "holiday" in forecast.day_kinds(date(2027, 3, 20), rainy)
    assert forecast.day_kinds(date(2026, 11, 2), rainy) == ["weekday_0"]
