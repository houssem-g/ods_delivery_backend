"""getOfferRank, updateOrderOffer and the rank in createOrderOffer's answer: where a courier's
price stands among the other couriers' pending offers, never their prices."""

import json
from datetime import timedelta

import pytest

from app.models import OrderOffer
from app.services import offers as offers_service
from app.services.offers import MAX_EDITS
from tests.factories import auth
from tests.order_helpers import OrderWorld, age, device, notifications, now, pushes, reload


@pytest.fixture
async def world(factory):
    return await OrderWorld(factory).setup()


@pytest.fixture
def offer_events(monkeypatch) -> list[tuple[str, str, str]]:
    seen: list[tuple[str, str, str]] = []
    original = offers_service.emit

    def record(session, entity, type_, id_, audience=None):
        seen.append((entity, type_, str(id_)))
        original(session, entity, type_, id_, audience)

    monkeypatch.setattr(offers_service, "emit", record)
    return seen


async def call(client, user, name, payload=None):
    return await client.post(f"/api/functions/{name}", json=payload or {}, headers=auth(user))


async def rivals(world, factory, order, *fees):
    """Other couriers' pending offers on `order`, one per fee."""
    made = []
    for n, fee in enumerate(fees):
        user = await factory.user(email=f"rival{n}@example.test", profile=False)
        courier = await world.make_courier(user, display_name=f"Rival {n}")
        made.append(await world.offer(order, courier, fee=fee))
    return made


# --- getOfferRank --------------------------------------------------------------------------------


async def test_rank_alone_and_by_typed_fee(client, world, factory):
    order = await world.order()
    alone = await call(client, world.courier_user, "getOfferRank", {"order_id": str(order.id), "fee": 5})
    assert alone.status_code == 200, alone.text
    assert alone.json() == {
        "success": True, "rank": 1, "total": 1, "cheapest": True, "tied": 0, "my_offer": None,
        "other_fees": [], "lowest_other": None,
    }  # fmt: skip

    await rivals(world, factory, order, "4", "6", "6", "9.5")
    for fee, rank, cheapest, tied in [
        (3.999, 1, True, 0),
        (4, 1, True, 1),
        (5, 2, False, 0),
        (6, 2, False, 2),
        (6.001, 4, False, 0),
        (10, 5, False, 0),
    ]:
        body = (
            await call(client, world.courier_user, "getOfferRank", {"order_id": str(order.id), "fee": fee})
        ).json()
        assert (body["rank"], body["total"], body["cheapest"], body["tied"]) == (rank, 5, cheapest, tied), fee
        assert body["other_fees"] == [4.0, 6.0, 6.0, 9.5] and body["lowest_other"] == 4.0


async def test_rank_of_my_offer_ignores_closed_offers_and_names_nobody(client, world, factory):
    order = await world.order(status="offers_received")
    mine = await world.offer(order, fee="7")
    cheaper, *_ = await rivals(world, factory, order, "6.5", "8", "1")
    body = (await call(client, world.courier_user, "getOfferRank", {"order_id": str(order.id)})).json()
    assert body["rank"] == 3 and body["total"] == 4 and body["my_offer"] == {"id": str(mine.id), "fee": 7.0}

    # withdrawn / rejected / expired offers of others do not count
    for status in ("withdrawn", "expired"):
        u = await factory.user(email=f"{status}@example.test", profile=False)
        await world.offer(order, await world.make_courier(u), fee="0.5", status=status)
    same = (await call(client, world.courier_user, "getOfferRank", {"order_id": str(order.id)})).json()
    assert same["rank"] == 3 and same["total"] == 4

    # the others' prices come back anonymous (the prices to beat), never an id or a name;
    # closed offers are not among them
    assert same["other_fees"] == [1.0, 6.5, 8.0] and same["lowest_other"] == 1.0
    text = json.dumps(same)
    assert str(cheaper.id) not in text and str(cheaper.courier_id) not in text and "Rival" not in text
    assert set(same) == {
        "success",
        "rank",
        "total",
        "cheapest",
        "tied",
        "my_offer",
        "other_fees",
        "lowest_other",
    }


async def test_rank_batch_for_my_offers_list(client, world, factory):
    first = await world.order(status="offers_received")
    second = await world.order(status="offers_received")
    closed = await world.order(status="accepted", courier=world.courier)
    no_offer = await world.order()
    await world.offer(first, fee="5")
    await world.offer(second, fee="3")
    await world.offer(closed, fee="3", status="accepted")
    await rivals(world, factory, first, "4")
    ids = [str(o.id) for o in (first, second, closed, no_offer)] + ["not-a-uuid"]
    body = (await call(client, world.courier_user, "getOfferRank", {"order_ids": ids})).json()
    assert set(body["ranks"]) == {str(first.id), str(second.id)}
    assert body["ranks"][str(first.id)]["rank"] == 2 and body["ranks"][str(first.id)]["total"] == 2
    assert body["ranks"][str(second.id)]["cheapest"] is True
    assert body["ranks"][str(second.id)]["my_offer"]["fee"] == 3.0
    too_many = await call(client, world.courier_user, "getOfferRank", {"order_ids": ids * 5})
    assert too_many.status_code == 400 and too_many.json() == {"error": "invalid_order_ids", "max": 20}
    assert (await call(client, world.courier_user, "getOfferRank", {"order_ids": []})).json() == {
        "success": True,
        "ranks": {},
    }


async def test_rank_guards(client, world, factory):
    order = await world.order()
    r = await call(client, world.customer, "getOfferRank", {"order_id": str(order.id), "fee": 5})
    assert r.status_code == 403 and r.json() == {"error": "courier_profile_missing"}
    pending_user = await factory.user(email="p@example.test", profile=False)
    await world.make_courier(pending_user, verification="pending")
    r = await call(client, pending_user, "getOfferRank", {"order_id": str(order.id), "fee": 5})
    assert r.status_code == 403 and r.json() == {"error": "courier_not_verified"}
    assert (await call(client, world.courier_user, "getOfferRank", {})).json() == {
        "error": "Missing order_id"
    }
    for fee in (0, 201, "abc"):
        r = await call(client, world.courier_user, "getOfferRank", {"order_id": str(order.id), "fee": fee})
        assert r.status_code == 400 and r.json() == {"error": "invalid_fee"}, fee
    r = await call(client, world.courier_user, "getOfferRank", {"order_id": "nope", "fee": 5})
    assert r.status_code == 404 and r.json() == {"error": "order_not_found"}
    r = await call(client, world.courier_user, "getOfferRank", {"order_id": str(order.id)})
    assert r.status_code == 404 and r.json() == {"error": "offer_not_found"}
    own = await world.order(world.courier_user)
    r = await call(client, world.courier_user, "getOfferRank", {"order_id": str(own.id), "fee": 5})
    assert r.status_code == 403 and r.json() == {"error": "own_order"}
    for status in ("accepted", "cancelled", "delivered"):
        closed = await world.order(status=status, courier=None if status == "cancelled" else world.courier)
        r = await call(client, world.courier_user, "getOfferRank", {"order_id": str(closed.id), "fee": 5})
        assert r.status_code == 409 and r.json() == {"error": "order_not_open", "status": status}
    r = await client.post("/api/functions/getOfferRank", json={"order_id": str(order.id)})
    assert r.status_code == 401


async def test_rank_is_rate_limited_per_courier(client, world, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "RATE_LIMIT_OFFER_RANK", "3/minute")
    order = await world.order()
    payload = {"order_id": str(order.id), "fee": 5}
    codes = [(await call(client, world.courier_user, "getOfferRank", payload)).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    r = await call(client, world.courier_user, "getOfferRank", payload)
    # never the generic limiter's words: the front would pause every poll
    assert r.json() == {"error": "too_many_rank_requests", "limit": "3/minute"}
    assert "rate limit" not in r.text.lower()


# --- createOrderOffer answers the rank -----------------------------------------------------------


async def test_create_offer_answers_its_rank(client, world, factory):
    order = await world.order()
    await rivals(world, factory, order, "4", "7")
    r = await call(client, world.courier_user, "createOrderOffer", {"order_id": str(order.id), "fee": 5})
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["rank"], body["total"], body["cheapest"], body["tied"]) == (2, 3, False, 0)
    assert body["offer"]["proposed_fee"] == 5


# --- updateOrderOffer ----------------------------------------------------------------------------


async def test_update_offer_changes_price_rank_and_tells_the_customer(client, world, factory, offer_events):
    order = await world.order(status="offers_received")
    mine = await world.offer(order, fee="7")
    await rivals(world, factory, order, "5", "6")
    await device(world.customer)
    r = await call(
        client,
        world.courier_user,
        "updateOrderOffer",
        {"offer_id": str(mine.id), "fee": 4.5555, "eta_minutes": 15, "message": " Je passe vite "},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True and (body["rank"], body["total"], body["cheapest"]) == (1, 3, True)
    assert body["offer"]["proposed_fee"] == 4.556 and body["offer"]["eta_minutes"] == 15
    assert body["offer"]["message"] == "Je passe vite" and body["offer"]["status"] == "pending"
    row = await reload(OrderOffer, mine.id)
    assert str(row.proposed_fee) == "4.556" and row.eta_minutes == 15
    assert ("OrderOffer", "update", str(mine.id)) in offer_events

    [note] = await notifications(world.customer, "new_offer")
    assert note.title_fr == "Offre modifiée" and note.title_ar == "تم تعديل العرض"
    assert note.body_fr == "Karim Trabelsi a modifié son offre : 4.556 DT · ~15 min"
    assert note.body_ar == "Karim Trabelsi عدّل عرضه: 4.556 د.ت · ~15 دق"
    assert note.order_id == order.id and note.data["kind"] == "offer_updated"
    assert note.data["offer_id"] == str(mine.id) and note.data["previous_fee"] == 7.0
    assert note.data["recipient_role"] == "customer"
    assert len(await pushes(world.customer)) == 1

    # the customer's OrderOffers list reads the new price
    q = json.dumps({"order_id": str(order.id)})
    listed = await client.get("/api/entities/OrderOffer", params={"q": q}, headers=auth(world.customer))
    doc = next(d for d in listed.json() if d["id"] == str(mine.id))
    assert doc["proposed_fee"] == 4.556


async def test_update_offer_push_is_throttled_and_edits_are_capped(client, world):
    order = await world.order(status="offers_received")
    mine = await world.offer(order, fee="7")
    await device(world.customer)
    payload = {"offer_id": str(mine.id)}
    assert (
        await call(client, world.courier_user, "updateOrderOffer", {**payload, "fee": 6})
    ).status_code == 200
    assert (
        await call(client, world.courier_user, "updateOrderOffer", {**payload, "fee": 5})
    ).status_code == 200
    assert len(await notifications(world.customer, "new_offer")) == 2
    assert len(await pushes(world.customer)) == 1  # the second change within 2 minutes: in-app only

    for note in await notifications(world.customer, "new_offer"):
        await age("notifications", note.id, created_at=now() - timedelta(minutes=3))
    assert (
        await call(client, world.courier_user, "updateOrderOffer", {**payload, "fee": 4})
    ).status_code == 200
    assert len(await pushes(world.customer)) == 2

    # the same values again: answered, nothing written, not counted
    same = await call(client, world.courier_user, "updateOrderOffer", {**payload, "fee": 4})
    assert same.status_code == 200 and same.json()["offer"]["proposed_fee"] == 4
    assert len(await notifications(world.customer, "new_offer")) == 3

    for n in range(MAX_EDITS - 3):
        r = await call(client, world.courier_user, "updateOrderOffer", {**payload, "fee": 10 + n})
        assert r.status_code == 200, r.text
    r = await call(client, world.courier_user, "updateOrderOffer", {**payload, "fee": 3})
    assert r.status_code == 429 and r.json() == {"error": "too_many_edits", "max": MAX_EDITS}
    assert (await reload(OrderOffer, mine.id)).proposed_fee == 10 + MAX_EDITS - 4


async def test_update_offer_keeps_eta_and_message_unless_sent(client, world):
    order = await world.order(status="offers_received")
    mine = await world.offer(order, fee="7")
    r = await call(client, world.courier_user, "updateOrderOffer", {"offer_id": str(mine.id), "fee": 6})
    assert r.json()["offer"]["eta_minutes"] == 20
    r = await call(
        client,
        world.courier_user,
        "updateOrderOffer",
        {"offer_id": str(mine.id), "fee": 6, "message": "Salut"},
    )
    assert r.json()["offer"]["message"] == "Salut"
    r = await call(
        client,
        world.courier_user,
        "updateOrderOffer",
        {"offer_id": str(mine.id), "fee": 6, "message": "", "eta_minutes": 9999},
    )
    assert r.json()["offer"]["message"] is None and r.json()["offer"]["eta_minutes"] == 20


async def test_update_offer_guards(client, world, factory):
    order = await world.order(status="offers_received")
    mine = await world.offer(order, fee="7")
    payload = {"offer_id": str(mine.id), "fee": 5}
    assert (await call(client, world.courier_user, "updateOrderOffer", {"fee": 5})).json() == {
        "error": "Missing offer_id"
    }
    for fee in (0, -2, 200.5, "x", None):
        r = await call(client, world.courier_user, "updateOrderOffer", {**payload, "fee": fee})
        assert r.status_code == 400 and r.json() == {"error": "invalid_fee"}, fee
    r = await call(client, world.customer, "updateOrderOffer", payload)
    assert r.status_code == 403 and r.json() == {"error": "courier_profile_missing"}
    # another courier's offer: as if it did not exist
    other_user = await factory.user(email="other@example.test", profile=False)
    await world.make_courier(other_user)
    r = await call(client, other_user, "updateOrderOffer", payload)
    assert r.status_code == 404 and r.json() == {"error": "offer_not_found"}
    r = await call(client, world.courier_user, "updateOrderOffer", {"offer_id": "nope", "fee": 5})
    assert r.status_code == 404 and r.json() == {"error": "offer_not_found"}

    withdrawn = await world.offer(await world.order(status="offers_received"), status="withdrawn")
    r = await call(client, world.courier_user, "updateOrderOffer", {"offer_id": str(withdrawn.id), "fee": 5})
    assert r.status_code == 404
    rejected = await world.offer(await world.order(status="offers_received"), status="rejected")
    r = await call(client, world.courier_user, "updateOrderOffer", {"offer_id": str(rejected.id), "fee": 5})
    assert r.status_code == 409 and r.json() == {"error": "offer_not_pending"}
    cancelled = await world.order(status="cancelled")
    stale = await world.offer(cancelled)
    r = await call(client, world.courier_user, "updateOrderOffer", {"offer_id": str(stale.id), "fee": 5})
    assert r.status_code == 409 and r.json() == {"error": "order_not_open", "status": "cancelled"}

    assert (await reload(OrderOffer, mine.id)).proposed_fee == 7
    assert await notifications(world.customer) == []
