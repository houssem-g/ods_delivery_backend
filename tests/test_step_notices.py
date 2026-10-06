"""Server-side order notices (app/services/step_notices.py) and the duplicate guard of
sendNotificationIfEnabled: each step produces exactly one notification even when the installed
app also sends it."""

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select, update

from app.db import SessionLocal
from app.models import PushDelivery, User
from app.services import push
from app.services.push import PushMessage
from tests.factories import auth
from tests.order_helpers import OrderWorld, age, device, notifications, now, pushes, rows


@pytest.fixture
async def world(factory):
    w = await OrderWorld(factory).setup()
    w.rival_user = await factory.user(email="rival@example.test", full_name="Sami", profile=False)
    w.rival = await w.make_courier(w.rival_user, display_name="Sami")
    return w


async def call(client, user, name: str, payload: dict[str, Any] | None = None):
    return await client.post(f"/api/functions/{name}", json=payload or {}, headers=auth(user))


async def app_sends(client, sender, recipient, order, type_: str, **metadata: Any):
    """What orderFlow.notifyUser of an installed app sends after its call."""
    return await call(
        client,
        sender,
        "sendNotificationIfEnabled",
        {
            "userId": recipient.email,
            "order_id": str(order.id),
            "type": type_,
            "title_fr": "t",
            "title_ar": "t",
            "body_fr": "b",
            "body_ar": "b",
            "metadata": metadata,
        },
    )


async def step(client, world, order, body):
    return await client.patch(f"/api/entities/Order/{order.id}", json=body, headers=auth(world.courier_user))


async def set_prefs(user: User, **values: Any) -> None:
    async with SessionLocal() as s:
        await s.execute(update(User).where(User.id == user.id).values(**values))
        await s.commit()


async def test_new_offer_is_sent_once_per_offer(client, world):
    order = await world.order()
    await device(world.customer)
    r = await call(
        client,
        world.courier_user,
        "createOrderOffer",
        {"order_id": str(order.id), "fee": 4.11, "eta_minutes": 25},
    )
    assert r.status_code == 200, r.text
    offer_id = r.json()["offer"]["id"]
    [note] = await notifications(world.customer, "new_offer")
    assert note.title_fr == "Nouvelle offre" and note.title_ar == "عرض جديد"
    assert note.body_fr == "Karim T. propose 4.110 DT · ~25 min"
    assert note.body_ar == "Karim T. يقترح 4.110 د.ت · ~25 دق"
    assert note.data["offer_id"] == offer_id and note.data["proposed_fee"] == 4.11
    assert len(await pushes(world.customer)) == 1

    # the installed app sends it again: skipped, the existing row is answered
    dup = await app_sends(client, world.courier_user, world.customer, order, "new_offer", offer_id=offer_id)
    assert dup.status_code == 200
    assert dup.json() == {"success": True, "skipped": "duplicate", "notification_id": str(note.id)}
    assert len(await notifications(world.customer, "new_offer")) == 1
    assert len(await pushes(world.customer)) == 1

    # another courier's offer a few seconds later is another notice
    r2 = await call(client, world.rival_user, "createOrderOffer", {"order_id": str(order.id), "fee": 5})
    assert r2.status_code == 200
    notes = await notifications(world.customer, "new_offer")
    assert len(notes) == 2 and notes[1].body_fr == "Sami propose 5.000 DT"


async def test_new_offer_push_follows_the_preferences(client, world):
    order = await world.order()
    await device(world.customer)
    await set_prefs(world.customer, notify_order_status=False)
    assert (
        await call(client, world.courier_user, "createOrderOffer", {"order_id": str(order.id), "fee": 4})
    ).status_code == 200
    assert len(await notifications(world.customer, "new_offer")) == 1  # in-app row always
    assert await pushes(world.customer) == []


async def test_order_accepted_is_pushed_on_the_vibrating_channel(client, world):
    order = await world.order(status="offers_received", items="Lait, Pain")
    offer = await world.offer(order, fee="6")
    await device(world.courier_user)
    await set_prefs(world.courier_user, notify_order_status=False)  # always pushed anyway
    r = await call(
        client, world.customer, "acceptOrderOffer", {"order_id": str(order.id), "offer_id": str(offer.id)}
    )
    assert r.status_code == 200, r.text
    [note] = await notifications(world.courier_user, "order_accepted")
    assert note.title_fr == "🎉 Offre acceptée"
    assert note.body_fr == "Offre acceptée — Monoprix : Lait, Pain. Allez au magasin."
    assert note.body_ar == "تم قبول عرضك — Monoprix: Lait, Pain. توجّه إلى المتجر."
    assert note.data["recipient_role"] == "courier" and note.data["offer_id"] == str(offer.id)
    [sent] = await pushes(world.courier_user)
    assert sent.status == "sent"
    msg = PushMessage(type="order_accepted", title_ar="a", title_fr="f", body_ar="", body_fr="", order_id="o")
    assert push.build_multicast(["t"], "fr", msg).android.notification.channel_id == "new_orders"

    dup = await app_sends(
        client, world.customer, world.courier_user, order, "order_accepted", offer_id=str(offer.id)
    )
    assert dup.json()["skipped"] == "duplicate"
    assert len(await notifications(world.courier_user)) == 1


async def test_each_step_one_notice_even_with_the_app_resending(client, world):
    await device(world.customer)
    order = await world.order(status="accepted", courier=world.courier, fee="7", eta_minutes=12)
    for body, type_ in (
        ({"status": "at_shop"}, "at_shop"),
        ({"status": "purchased", "purchase_amount": 20}, "purchased"),
        ({"status": "on_the_way"}, "on_the_way"),
        ({"status": "delivered"}, "delivered"),
    ):
        r = await step(client, world, order, body)
        assert r.status_code == 200, r.text
        legacy = "courier_on_way" if type_ == "on_the_way" else type_  # a synonym is the same notice
        dup = await app_sends(client, world.courier_user, world.customer, order, legacy, status=type_)
        assert dup.json()["skipped"] == "duplicate", type_
        assert len(await notifications(world.customer, type_)) == 1, type_
    notes = {n.type: n for n in await notifications(world.customer)}
    assert notes["at_shop"].body_fr == "Le livreur est chez Monoprix et fait vos achats"
    assert notes["purchased"].body_fr == "Montant des achats : 20.000 DT (selon le reçu)"
    assert notes["on_the_way"].body_fr == "Arrivée dans ~3 min. Préparez le paiement en espèces."  # the ride, not the offer's 12 min (B7)
    assert notes["delivered"].body_fr == "Votre commande a été livrée (27.000 DT). Notez votre livreur !"
    # each step names the order's shop: with two orders the customer knows which one moves (B60)
    assert notes["on_the_way"].title_fr == "🚚 Le livreur est en route · Monoprix"
    assert notes["on_the_way"].title_ar == "🚚 المندوب في الطريق إليك · Monoprix"
    assert notes["purchased"].title_fr == "🛍️ Achat effectué · Monoprix"
    assert notes["delivered"].title_fr == "✨ Commande livrée · Monoprix"
    assert len(await pushes(world.customer)) == 4


async def test_delivered_is_pushed_whatever_the_preferences(client, world):
    await device(world.customer)
    await set_prefs(world.customer, notify_order_status=False)
    order = await world.order(status="on_the_way", courier=world.courier, fee="5", purchase="10")
    await step(client, world, order, {"status": "at_shop"})  # refused step: nothing sent
    assert (await step(client, world, order, {"status": "delivered"})).status_code == 200
    assert [n.type for n in await notifications(world.customer)] == ["delivered"]
    assert len(await pushes(world.customer)) == 1


async def test_back_to_at_shop_after_a_price_check_is_not_an_arrival(client, world):
    order = await world.order(status="at_shop", courier=world.courier, fee="5")
    assert (await step(client, world, order, {"status": "price_confirmation_needed"})).status_code == 200
    assert (await step(client, world, order, {"status": "at_shop"})).status_code == 200
    assert await notifications(world.customer) == []


async def test_duplicate_window_and_guards(client, world):
    order = await world.order(status="accepted", courier=world.courier, fee="5")
    assert (await step(client, world, order, {"status": "at_shop"})).status_code == 200
    [note] = await notifications(world.customer, "at_shop")
    # a stranger is still refused (the guard runs before the duplicate check)
    stranger = await world.factory.user(email="x@example.test")
    assert (await app_sends(client, stranger, world.customer, order, "at_shop")).status_code == 403
    # past 120 s the same notice is written again
    await age("notifications", note.id, created_at=now() - timedelta(seconds=121))
    again = await app_sends(client, world.courier_user, world.customer, order, "at_shop")
    assert again.status_code == 200 and "skipped" not in again.json()
    assert len(await notifications(world.customer, "at_shop")) == 2
    # the customer writing it into his own list is deduplicated too
    r = await app_sends(client, world.customer, world.customer, order, "at_shop")  # self: allowed
    assert r.json()["skipped"] == "duplicate"


async def test_a_failing_notice_never_fails_the_step(client, world, monkeypatch):
    from app.services import step_notices

    async def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("push provider down")

    monkeypatch.setattr(step_notices, "notify", boom)
    order = await world.order(status="accepted", courier=world.courier, fee="5")
    r = await step(client, world, order, {"status": "at_shop"})
    assert r.status_code == 200 and r.json()["status"] == "at_shop"
    assert await notifications(world.customer) == []
    assert await rows(select(PushDelivery)) == []


async def test_server_side_notices_are_idempotent(world):
    from app.db import transaction
    from app.models import Order
    from app.services import step_notices

    order = await world.order(status="on_the_way", courier=world.courier, fee="5", purchase="10")
    async with transaction() as session:
        row = await session.get(Order, order.id)
        assert await step_notices.courier_step(session, row, "purchased", "on_the_way") is True
        assert await step_notices.courier_step(session, row, "purchased", "on_the_way") is False
        assert await step_notices.courier_step(session, row, "on_the_way", "client_no_response") is False
    assert len(await notifications(world.customer, "on_the_way")) == 1
