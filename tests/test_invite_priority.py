"""The invite-link head start (owner 18/10/2026, app/services/invite_priority.py): a customer signed
up through a courier's link → that courier alone sees and may offer on the order for 60 s, then the
normal broadcast; the customer's list (and the couriers' board) pins his offer on top."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update

from app.config import settings
from app.db import SessionLocal
from app.jobs.orders import invite_priority_release
from app.jobs.registry import JOBS
from app.jobs.scheduler import run_job
from app.models import Order, User, UserBlock
from tests.factories import auth
from tests.launch_helpers import set_launch_mode
from tests.order_helpers import OrderWorld, notifications, reload
from tests.test_order_flow_functions import order_form


@pytest.fixture
async def world(factory):
    return await OrderWorld(factory).setup()


async def call(client, user, name, payload=None):
    return await client.post(f"/api/functions/{name}", json=payload or {}, headers=auth(user))


async def invited(world, factory, email="sami@example.test", **courier_fields):
    """A second courier near the shop, the world's customer signed up through his link."""
    user = await factory.user(email=email, full_name="Sami", profile=False)
    courier = await world.make_courier(user, display_name="Sami Gharbi", **courier_fields)
    at = datetime.now(UTC)
    async with SessionLocal() as s:
        customer = await s.get(User, world.customer.id)
        customer.referred_by_courier_id, customer.referred_at, customer.profile_created_at = (
            courier.id,
            at,
            at,
        )
        await s.commit()
    return user, courier


async def place(client, world, **form):
    response = await call(client, world.customer, "placeOrder", {"order": order_form(**form)})
    assert response.status_code == 200, response.text
    return response.json()


async def open_orders(client, user):
    q = json.dumps({"status": {"$in": ["pending", "offers_received"]}})
    response = await client.get("/api/entities/Order", params={"q": q}, headers=auth(user))
    assert response.status_code == 200, response.text
    return {doc["id"] for doc in response.json()}


async def end_window(order_id):
    async with SessionLocal() as s:
        await s.execute(
            update(Order)
            .where(Order.id == order_id)
            .values(priority_until=datetime.now(UTC) - timedelta(seconds=1))
        )
        await s.commit()


async def allow_redispatch(order_id):
    """The customer's « relancer » waits 5 min after a broadcast: the test skips the wait."""
    async with SessionLocal() as s:
        await s.execute(update(Order).where(Order.id == order_id).values(last_dispatched_at=None))
        await s.commit()


async def offer(client, user, order_id, fee=5, **extra):
    return await call(client, user, "createOrderOffer", {"order_id": order_id, "fee": fee, **extra})


# --- the window --------------------------------------------------------------------------------------


async def test_only_the_invite_courier_hears_about_it_for_60_s(client, world, factory):
    sami_user, sami = await invited(world, factory)
    body = await place(client, world)
    order = body["order"]
    assert body["dispatched"] == 1
    [note] = await notifications(sami_user, "new_order")
    assert note.title_fr == "⭐ Votre client Amel a passé une commande"
    assert "60 s rien que pour vous" in note.body_fr and "60 ثانية" in note.body_ar
    assert note.data["preferred_courier"] is True and note.data["priority_until"].endswith("Z")
    assert await notifications(world.courier_user, "new_order") == []

    row = await reload(Order, order["id"])
    left = (row.priority_until - datetime.now(UTC)).total_seconds()
    assert row.priority_courier_id == sami.id and 55 < left <= 60 and row.priority_released_at is None
    # the customer's screen: « Votre livreur Sami a 60 s pour vous répondre en premier »
    assert order["priority_courier_name"] == "Sami" and order["priority_until"]
    assert order["priority_courier_id"] == str(sami.id)


async def test_the_others_do_not_see_it_during_the_window(client, world, factory):
    sami_user, _ = await invited(world, factory)
    order_id = (await place(client, world))["order"]["id"]
    other = world.courier_user
    assert order_id not in await open_orders(client, other)
    assert (await client.get(f"/api/entities/Order/{order_id}", headers=auth(other))).status_code == 404
    refused = await offer(client, other, order_id)
    assert refused.status_code == 404 and refused.json()["error"] == "order_not_found"  # neutral
    rank = await call(client, other, "getOfferRank", {"order_id": order_id, "fee": 5})
    assert rank.status_code == 404 and rank.json()["error"] == "order_not_found"
    intent = await call(client, other, "signalOfferIntent", {"order_id": order_id, "active": True})
    assert intent.status_code == 404
    # him: the order, and his offer goes through
    assert order_id in await open_orders(client, sami_user)
    doc = (await client.get(f"/api/entities/Order/{order_id}", headers=auth(sami_user))).json()
    assert doc["priority_until"] and doc["priority_courier_name"] == "Sami"
    assert (await offer(client, sami_user, order_id, fee=6)).status_code == 200
    # the 60 s go on after his offer: still nobody else
    assert order_id not in await open_orders(client, other)
    assert (await offer(client, other, order_id)).status_code == 404
    # a re-broadcast during the window tells nobody
    await allow_redispatch(order_id)
    again = await call(client, world.customer, "dispatchOrderToCouriers", {"order_id": order_id})
    assert again.json()["reason"] == "invite_priority" and again.json()["dispatched"] == 0
    assert await notifications(other, "new_order") == []
    # an admin sees everything
    assert order_id in await open_orders(client, world.admin)


async def test_end_of_window_broadcasts_once(client, world, factory):
    sami_user, _ = await invited(world, factory)
    order_id = (await place(client, world))["order"]["id"]
    assert await invite_priority_release() == {}  # not due yet
    await end_window(order_id)
    # visible from priority_until on, before the job even ran
    assert order_id in await open_orders(client, world.courier_user)
    assert await invite_priority_release() == {"due": 1, "released": 1, "errors": 0}
    assert len(await notifications(world.courier_user, "new_order")) == 1
    assert len(await notifications(sami_user, "new_order")) == 1  # not told twice
    row = await reload(Order, order_id)
    assert row.priority_released_at is not None
    assert (await offer(client, world.courier_user, order_id)).status_code == 200
    # idempotent: a second run (another leader, a retry) does nothing
    assert await invite_priority_release() == {}
    assert len(await notifications(world.courier_user, "new_order")) == 1
    # the customer's line is gone
    doc = (await client.get(f"/api/entities/Order/{order_id}", headers=auth(world.customer))).json()
    assert doc["priority_until"] is None and doc["priority_courier_name"] is None
    # never a second head start: a later re-broadcast is the normal one (him first, the others too)
    async with SessionLocal() as s:
        await s.execute(update(Order).where(Order.id == order_id).values(last_dispatched_at=None))
        await s.commit()
    again = (await call(client, world.customer, "dispatchOrderToCouriers", {"order_id": order_id})).json()
    assert again["dispatched"] == 2 and again["preferred_notified"] is True
    assert (await reload(Order, order_id)).priority_until == row.priority_until


async def test_restart_the_due_window_is_picked_up_by_the_next_leader(client, world, factory):
    """Nothing lives in memory: the window is in the database, any scheduler leader (after a restart)
    finds the due ones; a broadcast that happens first (a re-dispatch) counts as the release."""
    job = JOBS["invite_priority_release"]
    assert job.trigger.interval.total_seconds() == 5 and job.quiet
    await invited(world, factory)
    first = (await place(client, world))["order"]["id"]
    await end_window(first)
    result = await run_job(job)  # what the new leader runs
    assert result["ok"] and result["result"]["released"] == 1
    assert len(await notifications(world.courier_user, "new_order")) == 1

    # second order: the job is late, a re-dispatch comes first → it is the broadcast, the job skips it
    async with SessionLocal() as s:
        await s.execute(update(Order).values(status="cancelled", cancelled_at=datetime.now(UTC)))
        await s.commit()
    second = (await place(client, world))["order"]["id"]
    await end_window(second)
    async with SessionLocal() as s:
        await s.execute(update(Order).where(Order.id == second).values(last_dispatched_at=None))
        await s.commit()
    body = (await call(client, world.customer, "dispatchOrderToCouriers", {"order_id": second})).json()
    assert body["dispatched"] == 2
    assert await invite_priority_release() == {}
    assert len(await notifications(world.courier_user, "new_order")) == 2


async def test_window_ends_without_broadcast_once_taken(client, world, factory):
    sami_user, _ = await invited(world, factory)
    order_id = (await place(client, world))["order"]["id"]
    made = await offer(client, sami_user, order_id, fee=6)
    accepted = await call(
        client,
        world.customer,
        "acceptOrderOffer",
        {"order_id": order_id, "offer_id": made.json()["offer"]["id"]},
    )
    assert accepted.status_code == 200, accepted.text
    await end_window(order_id)
    assert (await invite_priority_release())["released"] == 1
    assert await notifications(world.courier_user, "new_order") == []
    assert (await reload(Order, order_id)).priority_released_at is not None


# --- invite courier unavailable → the normal broadcast at once ---------------------------------------


@pytest.mark.parametrize("case", ["busy", "not_verified", "blocked", "deleted", "pushes_off", "offline"])
async def test_unavailable_invite_courier_means_immediate_broadcast(client, world, factory, case):
    await set_launch_mode(case != "offline")
    fields = {"verification": "pending"} if case == "not_verified" else {}
    if case == "offline":
        fields["is_online"] = False
    sami_user, sami = await invited(world, factory, **fields)
    async with SessionLocal() as s:
        if case == "busy":
            pass
        elif case == "blocked":
            s.add(UserBlock(blocker_id=world.customer.id, blocked_id=sami_user.id))
        elif case == "deleted":
            await s.execute(update(User).where(User.id == sami_user.id).values(deleted_at=datetime.now(UTC)))
        elif case == "pushes_off":
            await s.execute(update(User).where(User.id == sami_user.id).values(notify_new_orders=False))
        await s.commit()
    if case == "busy":
        await world.order(status="at_shop", courier=sami)
    body = await place(client, world)
    order = await reload(Order, body["order"]["id"])
    assert order.priority_until is None and order.priority_courier_id is None
    assert len(await notifications(world.courier_user, "new_order")) == 1
    assert body["order"]["id"] in await open_orders(client, world.courier_user)
    sami_notes = await notifications(sami_user, "new_order")
    if case == "pushes_off":  # the in-app row as before, no head start (he would not hear it)
        assert len(sami_notes) == 1 and "rien que pour vous" not in sami_notes[0].body_fr
    else:
        assert sami_notes == []


async def test_no_attribution_no_head_start(client, world, factory):
    body = await place(client, world)
    assert (await reload(Order, body["order"]["id"])).priority_until is None
    assert len(await notifications(world.courier_user, "new_order")) == 1


# --- QA orders -------------------------------------------------------------------------------------


async def test_qa_order_head_start_then_qa_couriers_only(client, world, factory, monkeypatch):
    qa_user = await factory.user(email="qa-courier@example.test", full_name="QA", profile=False)
    await world.make_courier(qa_user, display_name="QA Livreur", phone_e164="+21655999888")
    sami_user, _ = await invited(world, factory, email="qa-sami@example.test")
    monkeypatch.setattr(settings, "QA_ACCOUNTS", ["qa-courier@example.test", "qa-sami@example.test"])
    body = await place(client, world, items_text="QA TEST pain")
    order_id = body["order"]["id"]
    assert body["dispatched"] == 1 and len(await notifications(sami_user, "new_order")) == 1
    assert await notifications(qa_user, "new_order") == []
    await end_window(order_id)
    await invite_priority_release()
    assert len(await notifications(qa_user, "new_order")) == 1
    assert await notifications(world.courier_user, "new_order") == []  # never a real courier


async def test_qa_order_with_a_non_qa_invite_courier_goes_to_qa_at_once(client, world, factory, monkeypatch):
    qa_user = await factory.user(email="qa-courier@example.test", full_name="QA", profile=False)
    await world.make_courier(qa_user, display_name="QA Livreur", phone_e164="+21655999888")
    sami_user, _ = await invited(world, factory)
    monkeypatch.setattr(settings, "QA_ACCOUNTS", ["qa-courier@example.test"])
    body = await place(client, world, items_text="QA TEST pain")
    assert (await reload(Order, body["order"]["id"])).priority_until is None
    assert len(await notifications(qa_user, "new_order")) == 1
    assert await notifications(sami_user, "new_order") == []


# --- the pinned offer --------------------------------------------------------------------------------


async def test_his_offer_is_pinned_for_the_customer_and_on_the_board(client, world, factory):
    sami_user, sami = await invited(world, factory)
    order_id = (await place(client, world))["order"]["id"]
    mine = await offer(client, sami_user, order_id, fee=9, eta_minutes=40)
    assert mine.status_code == 200, mine.text
    # him during the window: alone, pinned
    assert mine.json()["board"] == [
        {
            "who": "me",
            "fee": 9.0,
            "eta_minutes": 40,
            "fastest": False,
            "cheapest": False,
            "verified": True,
            "pinned": True,
        }
    ]
    await end_window(order_id)
    await invite_priority_release()
    third_user = await factory.user(email="third@example.test", full_name="Third", profile=False)
    await world.make_courier(third_user, display_name="Third")
    assert (await offer(client, world.courier_user, order_id, fee=5, eta_minutes=20)).status_code == 200
    assert (await offer(client, third_user, order_id, fee=6, eta_minutes=25)).status_code == 200

    other = (await call(client, world.courier_user, "getOfferRank", {"order_id": order_id})).json()
    whos = [(e["who"], e.get("pinned", False)) for e in other["board"]]
    assert whos[0] == ("client", True) and ("me", False) in whos and ("A", False) in whos
    assert "B" not in {w for w, _ in whos}  # letters only for the anonymous others
    assert other["rank"] == 1  # his price place is unchanged by the pin
    cheapest = [e for e in other["board"] if e["cheapest"]]
    assert [e["who"] for e in cheapest] == ["me"] and not other["board"][0]["cheapest"]
    his = (await call(client, sami_user, "getOfferRank", {"order_id": order_id})).json()
    assert his["board"][0]["who"] == "me" and his["board"][0]["pinned"] is True
    assert [e["who"] for e in his["board"][1:]] == ["A", "B"]

    q = json.dumps({"order_id": order_id})
    offers = (
        await client.get("/api/entities/OrderOffer", params={"q": q}, headers=auth(world.customer))
    ).json()
    flags = {o["courier_id"]: o["from_your_courier"] for o in offers}
    assert flags[str(sami.id)] is True and flags[str(world.courier.id)] is False
    # another courier never reads the flag (nor the others' offers)
    own = (
        await client.get(f"/api/entities/OrderOffer/{mine.json()['offer']['id']}", headers=auth(sami_user))
    ).json()
    assert own.get("from_your_courier") is None
