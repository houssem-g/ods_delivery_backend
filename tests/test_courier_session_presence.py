"""Owner, 10/10/2026: signing in puts a verified courier online, signing out puts him offline;
a token refresh changes nothing, an unverified courier stays offline."""

from sqlalchemy import update

from app.config import settings
from app.db import SessionLocal
from app.models import Courier
from tests.order_helpers import OrderWorld, reload
from tests.test_auth import login


async def set_offline(courier: Courier) -> None:
    async with SessionLocal() as s:
        await s.execute(
            update(Courier).where(Courier.id == courier.id).values(is_online=False, online_since=None)
        )
        await s.commit()


async def test_sign_in_online_sign_out_offline(client, factory):
    world = await OrderWorld(factory).setup()
    await set_offline(world.courier)
    assert (await login(client, world.courier_user.email)).status_code == 200
    on = await reload(Courier, world.courier.id)
    assert on.is_online is True and on.online_since is not None and on.last_seen_at is not None
    # a refresh is not a sign-in: the courier who went offline by hand stays offline
    await set_offline(world.courier)
    assert (await client.post("/api/auth/refresh")).status_code == 200
    assert (await reload(Courier, world.courier.id)).is_online is False
    await login(client, world.courier_user.email)
    assert (await client.post("/api/auth/logout")).status_code == 200
    off = await reload(Courier, world.courier.id)
    assert off.is_online is False and off.online_since is None


async def test_unverified_courier_stays_offline(client, factory):
    world = await OrderWorld(factory).setup()
    user = await factory.user(email="pending@example.test", profile=False)
    pending = await world.make_courier(user, verification="pending", is_online=False)
    assert (await login(client, user.email)).status_code == 200
    assert (await reload(Courier, pending.id)).is_online is False


async def test_customer_sign_in_and_unknown_logout(client, factory):
    user = await factory.user(email="just.customer@example.test")
    assert (await login(client, user.email)).status_code == 200
    client.cookies.set(settings.REFRESH_COOKIE_NAME, "not-a-token")
    assert (await client.post("/api/auth/logout")).status_code == 200
