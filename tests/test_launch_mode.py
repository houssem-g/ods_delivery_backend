"""Launch mode (owner, 13/10/2026): while couriers are few, every verified free courier hears about
new orders, even « Hors ligne » or signed out; an admin switches it off in Admin → Paramètres."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import DeviceToken, PushDelivery, User
from app.services.notifications import notify
from tests.factories import auth
from tests.launch_helpers import set_launch_mode
from tests.messaging_factories import make_device
from tests.order_helpers import OrderWorld, notifications


@pytest.fixture
async def world(factory):
    return await OrderWorld(factory).setup()


async def call(client, user, name, payload=None):
    return await client.post(f"/api/functions/{name}", json=payload or {}, headers=auth(user))


async def rows(model, *where):
    async with SessionLocal() as s:
        return list((await s.execute(select(model).where(*where))).scalars())


async def test_on_by_default_offline_and_positionless_couriers_are_notified(client, world, factory):
    async def courier(email, **fields):
        user = await factory.user(email=email, profile=False)
        await world.make_courier(user, **fields)
        return user

    offline = await courier("offline@example.test", is_online=False)
    never_opened = await courier("nopos@example.test", is_online=False, at=None)  # serves Sousse
    pending = await courier("pending@example.test", verification="pending", is_online=False)
    far = await courier("far@example.test", at=(35.95, 10.6), is_online=False)  # ~14 km
    other_gov = await courier("gov@example.test", service_governorate="Monastir", at=None, is_online=False)
    order = await world.order()
    body = (await call(client, world.customer, "dispatchOrderToCouriers", {"order_id": str(order.id)})).json()
    notified = {n.user_id for n in await notifications(type_="new_order")}
    assert notified == {world.courier_user.id, offline.id, never_opened.id}
    assert not notified & {pending.id, far.id, other_gov.id}
    assert body["dispatched"] == 3
    # a courier without a position gets the order without a distance
    [nopos_row] = [n for n in await notifications(type_="new_order") if n.user_id == never_opened.id]
    assert "km" not in (nopos_row.body_fr or "")


async def test_switched_off_only_online_couriers(client, world, factory):
    user = await factory.user(email="offline@example.test", profile=False)
    await world.make_courier(user, is_online=False)
    await set_launch_mode(False)
    order = await world.order()
    await call(client, world.customer, "dispatchOrderToCouriers", {"order_id": str(order.id)})
    assert {n.user_id for n in await notifications(type_="new_order")} == {world.courier_user.id}


async def test_preferred_courier_notified_even_offline(client, world, factory):
    referrer_user = await factory.user(email="ref@example.test", profile=False)
    referrer = await world.make_courier(referrer_user, is_online=False)
    now = datetime.now(UTC)
    async with SessionLocal() as s:
        customer = await s.get(User, world.customer.id)
        customer.referred_by_courier_id, customer.referred_at, customer.profile_created_at = (
            referrer.id,
            now,
            now,
        )
        await s.commit()
    order = await world.order(preferred_courier_id=referrer.id)
    body = (await call(client, world.customer, "dispatchOrderToCouriers", {"order_id": str(order.id)})).json()
    assert body["preferred_notified"] is True


async def _push(user_id, type_):
    async with SessionLocal() as s:
        await notify(s, user_id=user_id, type_=type_, title_fr="t", title_ar="t", body_fr="b", body_ar="b")
        await s.commit()
    return len(await rows(PushDelivery, PushDelivery.user_id == user_id))


async def test_signed_out_courier_keeps_only_new_orders_while_launch_is_on(client, world):
    await make_device(world.courier_user, token="fcm-courier")
    out = await call(client, world.courier_user, "unregisterDeviceToken", {"token": "fcm-courier"})
    assert out.json() == {"success": True, "deactivated": 1, "new_orders_kept": True}
    [token] = await rows(DeviceToken, DeviceToken.token == "fcm-courier")
    assert token.is_active and token.signed_out_at is not None
    assert await _push(world.courier_user.id, "new_order") == 1  # still told about new orders
    assert await _push(world.courier_user.id, "new_message") == 1  # but nothing else
    await set_launch_mode(False)
    assert await _push(world.courier_user.id, "new_order") == 1  # switched off: nothing any more
    # signing in again on the phone clears it
    again = await call(
        client, world.courier_user, "registerDeviceToken", {"token": "fcm-courier", "platform": "android"}
    )
    assert again.status_code == 200
    [token] = await rows(DeviceToken, DeviceToken.token == "fcm-courier")
    assert token.signed_out_at is None
    assert await _push(world.courier_user.id, "new_message") == 2


async def test_signed_out_customer_or_launch_off_stops_everything(client, world):
    await make_device(world.customer, token="fcm-customer")
    out = await call(client, world.customer, "unregisterDeviceToken", {"token": "fcm-customer"})
    assert out.json()["new_orders_kept"] is False
    [token] = await rows(DeviceToken, DeviceToken.token == "fcm-customer")
    assert token.is_active is False
    await set_launch_mode(False)
    await make_device(world.courier_user, token="fcm-c2")
    out = await call(client, world.courier_user, "unregisterDeviceToken", {"token": "fcm-c2"})
    assert out.json()["new_orders_kept"] is False


async def test_admin_switch(client, world):
    assert (await call(client, world.admin, "getDispatchSettings")).json() == {
        "success": True,
        "notify_offline_couriers": True,
    }
    off = await call(client, world.admin, "setDispatchSettings", {"notify_offline_couriers": False})
    assert off.json() == {"success": True, "notify_offline_couriers": False}
    assert (await call(client, world.admin, "getDispatchSettings")).json()["notify_offline_couriers"] is False
    assert (
        await call(client, world.courier_user, "setDispatchSettings", {"notify_offline_couriers": True})
    ).status_code == 403
    assert (await call(client, world.customer, "getDispatchSettings")).status_code == 403
    bad = await call(client, world.admin, "setDispatchSettings", {"notify_offline_couriers": "yes"})
    assert bad.status_code == 400
