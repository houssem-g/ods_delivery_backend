"""The courier's arrival at the customer (owner, 18/10/2026): courier → shop + the shop's average wait
+ shop → customer, computed by the server (app/services/arrival.py), shown in the offer sheet
(getOfferEstimate), stored on each offer (estimated_eta_minutes, to_shop_km, eta_announced) and used
by the ranking (getOfferRank: arrival rank + the anonymous board). OSRM off unless mocked."""

import json
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import update

from app.config import settings
from app.db import SessionLocal
from app.integrations import osrm
from app.models import Courier, Order, OrderOffer, OrderStatusEvent
from app.services import arrival
from tests.factories import auth
from tests.order_helpers import SOUSSE_SHOP, TUNIS, OrderWorld, notifications, now, pt, reload

PARIS = (48.85, 2.35)
NEAR = (35.8300, 10.6000)  # ~1 km from the shop


@pytest.fixture
async def world(factory):
    return await OrderWorld(factory).setup()


async def call(client, user, name, payload=None):
    return await client.post(f"/api/functions/{name}", json=payload or {}, headers=auth(user))


async def visits(world, *minutes: float, shop=SOUSSE_SHOP, stops: int = 1) -> None:
    """Past orders at `shop`: the courier arrived (at_shop) and left `minutes` later (purchased)."""
    for n, m in enumerate(minutes):
        order = await world.order(status="delivered", shop=shop, stops=stops, courier=world.courier)
        start = now() - timedelta(days=1, minutes=n * 90)
        async with SessionLocal() as s:
            s.add(OrderStatusEvent(order_id=order.id, to_status="at_shop", source="test", created_at=start))
            s.add(
                OrderStatusEvent(
                    order_id=order.id,
                    to_status="purchased",
                    source="test",
                    created_at=start + timedelta(minutes=m),
                )
            )
            await s.commit()


async def place(world, courier, at, seen_minutes_ago=1):
    async with SessionLocal() as s:
        await s.execute(
            update(Courier)
            .where(Courier.id == courier.id)
            .values(last_location=pt(*at), last_seen_at=now() - timedelta(minutes=seen_minutes_ago))
        )
        await s.commit()


# --- the shop's average wait --------------------------------------------------------------------------


async def test_wait_is_8_min_by_default_then_the_mean_of_real_visits(client, world):
    order = await world.order()
    body = (await call(client, world.courier_user, "getOfferEstimate", {"order_id": str(order.id)})).json()
    assert body["wait"] == {"minutes": 8, "source": "default", "samples": 0, "shops": 1}

    # two visits only: still the default; noise (20 s, 3 h) and multi-shop orders never count
    await visits(world, 4, 6)
    await visits(world, 1 / 3, 180)
    await visits(world, 2, 2, 2, stops=2)
    await visits(world, 1, 1, 1, shop=TUNIS)  # another shop
    arrival.wait_cache.clear()
    assert (await arrival_wait(order)) == (8, "default", 2)

    await visits(world, 8, 10)
    arrival.wait_cache.clear()
    assert (await arrival_wait(order)) == (7, "measured", 4)  # (4 + 6 + 8 + 10) / 4


async def test_wait_is_bounded(world):
    await visits(world, 60, 70, 80)
    order = await world.order()
    assert (await arrival_wait(order)) == (30, "measured", 3)
    await visits(world, 0.6, 0.6, 0.6, shop=TUNIS)
    in_tunis = await world.order(shop=TUNIS, delivery=(36.81, 10.19))
    assert (await arrival_wait(in_tunis)) == (arrival.WAIT_MIN, "measured", 3)


async def arrival_wait(order):
    async with SessionLocal() as s:
        found = await arrival.arrival(s, order.id, "scooter", None)
    assert found is not None
    return found.wait_min, found.wait_source, found.wait_samples


# --- getOfferEstimate -------------------------------------------------------------------------------


async def test_estimate_straight_line_by_vehicle(client, world):
    order = await world.order()
    body = (
        await call(
            client,
            world.courier_user,
            "getOfferEstimate",
            {"order_id": str(order.id), "from_lat": NEAR[0], "from_lng": NEAR[1]},
        )
    ).json()
    assert body["success"] is True and body["source"] == "straight_line" and body["vehicle"] == "scooter"
    # NEAR → shop 0.9 km × 1.3 = 1.17 km at 25 km/h (2.8 min); shop → home 1.15 km × 1.3 = 1.5 km (3.6 min)
    assert body["to_shop"] == {"distance_km": 1.17, "minutes": 3}
    assert body["delivery"] == {"distance_km": 1.5, "minutes": 4}
    assert body["total_minutes"] == 3 + 8 + 4 and body["announce_margin_minutes"] == 3

    async with SessionLocal() as s:
        await s.execute(update(Courier).where(Courier.id == world.courier.id).values(vehicle="walking"))
        await s.commit()
    walk = (
        await call(
            client,
            world.courier_user,
            "getOfferEstimate",
            {"order_id": str(order.id), "from_lat": NEAR[0], "from_lng": NEAR[1]},
        )
    ).json()
    assert (
        walk["vehicle"] == "walking"
        and walk["to_shop"]["minutes"] == 14
        and walk["delivery"]["minutes"] == 18
    )
    assert walk["total_minutes"] == 14 + 8 + 18


async def test_estimate_by_the_roads(client, world, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        legs = [
            {"duration": 300, "distance": 1200, "steps": []},
            {"duration": 400, "distance": 2300, "steps": []},
        ]
        return httpx.Response(200, json={"routes": [{"geometry": {"coordinates": []}, "legs": legs}]})

    monkeypatch.setattr(settings, "OSRM_URL", "http://osrm.test")
    monkeypatch.setattr(osrm, "transport", httpx.MockTransport(handler))
    order = await world.order()
    body = (
        await call(
            client,
            world.courier_user,
            "getOfferEstimate",
            {"order_id": str(order.id), "from_lat": NEAR[0], "from_lng": NEAR[1]},
        )
    ).json()
    # OSRM 5 min / 1.2 km (a scooter: 2.9 min, OSRM wins); 6.7 min / 2.3 km (scooter: 5.5 min)
    assert body["source"] == "osrm"
    assert body["to_shop"] == {"distance_km": 1.2, "minutes": 5}
    assert body["delivery"] == {"distance_km": 2.3, "minutes": 7}
    assert body["total_minutes"] == 20


async def test_estimate_position_rules(client, world, factory):
    order = await world.order()
    ask = {"order_id": str(order.id)}
    # no position sent: the server's recent one (the shop itself) → 0 min to the shop
    body = (await call(client, world.courier_user, "getOfferEstimate", ask)).json()
    assert body["to_shop"] == {"distance_km": 0.0, "minutes": 0} and body["total_minutes"] == 12
    # « abroad » while the server saw him at the shop a minute ago: that fix (no dodging « annoncé »)
    paris = (
        await call(
            client, world.courier_user, "getOfferEstimate", ask | {"from_lat": PARIS[0], "from_lng": PARIS[1]}
        )
    ).json()
    assert paris["to_shop"] == {"distance_km": 0.0, "minutes": 0} and paris["total_minutes"] == 12
    # a sent position 115 km from a fix 1 minute old is not his: the server's fix wins
    tunis = (
        await call(
            client, world.courier_user, "getOfferEstimate", ask | {"from_lat": TUNIS[0], "from_lng": TUNIS[1]}
        )
    ).json()
    assert tunis["to_shop"]["distance_km"] == 0.0
    # an old fix (2 h): the sent position is his
    await place(world, world.courier, SOUSSE_SHOP, seen_minutes_ago=120)
    far = (
        await call(
            client, world.courier_user, "getOfferEstimate", ask | {"from_lat": TUNIS[0], "from_lng": TUNIS[1]}
        )
    ).json()
    assert far["to_shop"]["distance_km"] > 100
    # really abroad (his address in Paris, no recent fix here): shop → customer only, no total
    abroad = (
        await call(
            client, world.courier_user, "getOfferEstimate", ask | {"from_lat": PARIS[0], "from_lng": PARIS[1]}
        )
    ).json()
    assert (
        abroad["to_shop"] is None and abroad["total_minutes"] is None and abroad["delivery"]["minutes"] == 4
    )
    # and no position at all: no courier → shop line
    alone = (await call(client, world.courier_user, "getOfferEstimate", ask)).json()
    assert alone["to_shop"] is None and alone["total_minutes"] is None

    stranger = await factory.user(email="stranger@example.test")
    assert (await call(client, stranger, "getOfferEstimate", ask)).status_code == 403
    assert (await call(client, world.courier_user, "getOfferEstimate", {})).status_code == 400
    closed = await world.order(status="cancelled")
    assert (
        await call(client, world.courier_user, "getOfferEstimate", {"order_id": str(closed.id)})
    ).status_code == 404


async def test_multi_shop_order_waits_at_each_shop(client, world):
    order = await world.order(stops=2)
    body = (await call(client, world.courier_user, "getOfferEstimate", {"order_id": str(order.id)})).json()
    assert body["wait"]["shops"] == 2 and body["wait"]["minutes"] == 16


# --- the offer stores it ------------------------------------------------------------------------------


async def test_offer_stores_the_estimate_and_the_delay_never_goes_under_it(client, world):
    """Owner 19/10/2026 (QA M12): he may ADD time to the computation, never remove any, not one minute."""
    order = await world.order()
    at = {"from_lat": NEAR[0], "from_lng": NEAR[1]}
    for typed in (12, 14):  # the former 2-minute tolerance is gone
        short = await call(
            client,
            world.courier_user,
            "createOrderOffer",
            {"order_id": str(order.id), "fee": 4, "eta_minutes": typed, **at},
        )
        assert short.status_code == 400
        assert short.json() == {"error": "eta_below_computed", "min_eta": 15, "computed": 15}
    sent = await call(
        client,
        world.courier_user,
        "createOrderOffer",
        {"order_id": str(order.id), "fee": 4, "eta_minutes": 15, **at},
    )
    assert sent.status_code == 200, sent.text
    offer = sent.json()["offer"]
    assert offer["estimated_eta_minutes"] == 15 and offer["to_shop_km"] == 1.17 and offer["eta_minutes"] == 15
    assert offer["eta_announced"] is False and offer["arrival_minutes"] == 15 and offer["arrival_verified"]
    row = await reload(OrderOffer, offer["id"])
    assert row.estimated_eta_minutes == 15

    refused = await call(
        client,
        world.courier_user,
        "updateOrderOffer",
        {"offer_id": offer["id"], "fee": 4, "eta_minutes": 14, **at},
    )
    assert refused.status_code == 400 and refused.json()["error"] == "eta_below_computed"
    assert (await reload(OrderOffer, offer["id"])).eta_minutes == 15  # nothing changed
    changed = await call(
        client,
        world.courier_user,
        "updateOrderOffer",
        {"offer_id": offer["id"], "fee": 4, "eta_minutes": 23, **at},
    )
    assert changed.status_code == 200 and changed.json()["my_eta"] == 23
    # the price alone may change without a delay (no floor check then)
    assert (
        await call(client, world.courier_user, "updateOrderOffer", {"offer_id": offer["id"], "fee": 4.5})
    ).status_code == 200
    # the customer reads the typed delay, « chez vous ≈ 23 min »
    note = (await notifications(world.customer, "new_offer"))[-1]
    assert "chez vous ≈ 23 min" in note.body_fr and "الوصول إليك حوالي 23 دقيقة" in note.body_ar


async def test_ranked_by_the_computed_arrival_never_the_typed_one(client, world, factory):
    order = await world.order()
    typed_fast = await rival(world, factory, order, 0, "3", 5, 6, est=None)  # A: typed 5, no computation
    computed = await rival(world, factory, order, 1, "3", 30, 5)  # B: typed 30, computed 25
    async with SessionLocal() as s:
        await s.execute(
            update(OrderOffer).where(OrderOffer.courier_id == computed.id).values(estimated_eta_minutes=25)
        )
        await s.commit()
    assert typed_fast  # an offer without a computation comes after every computed one
    body = (
        await call(
            client,
            world.courier_user,
            "getOfferRank",
            {"order_id": str(order.id), "fee": 4, "eta": 30, "estimate": 28},
        )
    ).json()
    assert [e["who"] for e in body["board"]] == ["B", "me", "A"]
    # each offer's arrival: B raised its 25 to 30, mine 28 → 30, A has no computation (its typed 5)
    assert [e["eta_minutes"] for e in body["board"]] == [30, 30, 5]
    assert (body["eta_rank"], body["eta_tied"], body["my_arrival"]) == (1, 1, 30)  # ex æquo with B
    assert (body["eta_rank_shown"], body["eta_tied_shown"]) == (1, 1)  # one rank (QA M12)
    # A shows 5 min (unverified): nobody is « le + rapide » (a computed 30 next to a shown 5)
    assert not any(e["fastest"] for e in body["board"])
    assert [e["verified"] for e in body["board"]] == [True, True, False]
    # without his computation (position unknown), his typed delay ranks him after the computed ones
    blind = (
        await call(
            client, world.courier_user, "getOfferRank", {"order_id": str(order.id), "fee": 4, "eta": 3}
        )
    ).json()
    assert [e["who"] for e in blind["board"]] == ["B", "me", "A"] and blind["eta_rank"] == 2
    assert blind["eta_rank_shown"] == 2


async def test_older_app_without_delay_gets_the_computed_one(client, world):
    order = await world.order()
    sent = await call(client, world.courier_user, "createOrderOffer", {"order_id": str(order.id), "fee": 4})
    offer = sent.json()["offer"]
    assert offer["eta_minutes"] == offer["estimated_eta_minutes"] == 12 and offer["eta_announced"] is False
    note = (await notifications(world.customer, "new_offer"))[-1]
    assert note.body_fr.endswith("· chez vous ≈ 12 min")


async def test_offer_without_any_position_has_no_estimate(client, world):
    await place(world, world.courier, SOUSSE_SHOP, seen_minutes_ago=600)
    order = await world.order()
    sent = await call(
        client,
        world.courier_user,
        "createOrderOffer",
        {"order_id": str(order.id), "fee": 4, "eta_minutes": 5},
    )
    offer = sent.json()["offer"]
    assert (
        offer["estimated_eta_minutes"] is None
        and offer["to_shop_km"] is None
        and offer["eta_announced"] is False
    )


# --- the ranking: price AND arrival, anonymous ------------------------------------------------------


async def rival(world, factory, order, n, fee, eta, minutes_ago, est="same"):
    """Another courier's pending offer; `est` its computed arrival (default: the same as its delay;
    None: no computation, his position was unknown)."""
    user = await factory.user(email=f"rival{n}@example.test", full_name=f"Rival Person {n}", profile=False)
    courier = await world.make_courier(user, display_name=f"Rival Person {n}", phone_e164=f"+2165000000{n}")
    offer = await world.offer(order, courier, fee=fee)
    async with SessionLocal() as s:
        await s.execute(
            update(OrderOffer)
            .where(OrderOffer.id == offer.id)
            .values(
                eta_minutes=eta,
                estimated_eta_minutes=eta if est == "same" else est,
                created_at=now() - timedelta(minutes=minutes_ago),
            )
        )
        await s.commit()
    return courier


async def test_board_sorted_like_the_customer_sees_it_and_anonymous(client, world, factory):
    order = await world.order(status="offers_received")
    rivals = [
        await rival(world, factory, order, 0, "3.5", 15, 5),  # A
        await rival(world, factory, order, 1, "2.5", 28, 4),  # B
        await rival(world, factory, order, 2, "4", 35, 3),  # C
    ]
    r = await call(
        client,
        world.courier_user,
        "getOfferRank",
        {"order_id": str(order.id), "fee": 2.48, "eta": 22, "estimate": 22},
    )
    body = r.json()
    assert (body["rank"], body["total"], body["eta_rank"], body["eta_tied"], body["my_eta"]) == (
        1,
        4,
        2,
        0,
        22,
    )
    assert body["board"] == [
        {"who": "A", "fee": 3.5, "eta_minutes": 15, "fastest": True, "cheapest": False, "verified": True},
        {"who": "me", "fee": 2.48, "eta_minutes": 22, "fastest": False, "cheapest": True, "verified": True},
        {"who": "B", "fee": 2.5, "eta_minutes": 28, "fastest": False, "cheapest": False, "verified": True},
        {"who": "C", "fee": 4.0, "eta_minutes": 35, "fastest": False, "cheapest": False, "verified": True},
    ]
    raw = r.text
    for c in rivals:  # never an id, a name, a phone or a position of another courier
        assert str(c.id) not in raw and c.display_name not in raw and c.phone_e164 not in raw
    assert "lat" not in raw and "lng" not in raw and "distance" not in raw

    # a tie at the same minute: the cheaper first; ex æquo counted
    tie = (
        await call(
            client,
            world.courier_user,
            "getOfferRank",
            {"order_id": str(order.id), "fee": 3.6, "eta": 15, "estimate": 15},
        )
    ).json()
    assert tie["eta_rank"] == 1 and tie["eta_tied"] == 1
    assert [e["who"] for e in tie["board"]] == ["A", "me", "B", "C"] and tie["board"][0]["fastest"]
    # last on both
    last = (
        await call(
            client,
            world.courier_user,
            "getOfferRank",
            {"order_id": str(order.id), "fee": 9, "eta": 50, "estimate": 50},
        )
    ).json()
    assert (last["rank"], last["eta_rank"]) == (4, 4) and last["board"][-1]["who"] == "me"
    assert (
        await call(
            client, world.courier_user, "getOfferRank", {"order_id": str(order.id), "fee": 3, "eta": 0}
        )
    ).status_code == 400


async def test_board_alone_and_my_sent_offer_keeps_its_place(client, world, factory):
    order = await world.order()
    alone = (
        await call(
            client,
            world.courier_user,
            "getOfferRank",
            {"order_id": str(order.id), "fee": 3, "eta": 20, "estimate": 20},
        )
    ).json()
    assert alone["board"] == [
        {"who": "me", "fee": 3.0, "eta_minutes": 20, "fastest": False, "cheapest": False, "verified": True}
    ]
    assert (alone["eta_rank"], alone["total"]) == (1, 1)

    await rival(world, factory, order, 0, "3", 20, 10)  # A, earlier
    mine = await world.offer(order, fee="3")  # eta 20, now
    async with SessionLocal() as s:
        await s.execute(update(OrderOffer).where(OrderOffer.id == mine.id).values(estimated_eta_minutes=20))
        await s.commit()
    await rival(world, factory, order, 1, "3", 20, -1)  # B, later than mine
    body = (await call(client, world.courier_user, "getOfferRank", {"order_id": str(order.id)})).json()
    assert body["my_eta"] == 20 and body["my_offer"]["id"] == str(mine.id)
    assert [e["who"] for e in body["board"]] == ["A", "me", "B"]
    assert [e["fastest"] for e in body["board"]] == [True, False, False]
    assert [e["cheapest"] for e in body["board"]] == [True, False, False]

    batch = (await call(client, world.courier_user, "getOfferRank", {"order_ids": [str(order.id)]})).json()
    one = batch["ranks"][str(order.id)]
    assert (one["eta_rank"], one["eta_tied"]) == (1, 2) and "board" not in one


async def test_an_offer_without_delay_ranks_last_on_arrival(client, world, factory):
    order = await world.order()
    await rival(world, factory, order, 0, "2", None, 5, est=None)
    body = (
        await call(
            client,
            world.courier_user,
            "getOfferRank",
            {"order_id": str(order.id), "fee": 3, "eta": 40, "estimate": 40},
        )
    ).json()
    assert body["eta_rank"] == 1 and [e["who"] for e in body["board"]] == ["me", "A"]
    assert body["board"][0]["fastest"] and body["board"][1]["cheapest"]


async def test_fastest_only_among_verified_and_never_beaten_by_a_shown_delay(client, world, factory):
    """The owner's capture (18/10/2026): him computed 22 min, « Livreur A » unverified 18 min, « Livreur
    B » unverified 30 min → nobody « le + rapide »; with A computed 25 (shown 25), he is."""
    order = await world.order()
    await rival(world, factory, order, 0, "3", 18, 5, est=None)  # A: shows 18, not verified
    await rival(world, factory, order, 1, "2.5", 30, 4, est=None)  # B: shows 30, not verified
    ask = {"order_id": str(order.id), "fee": 4.48, "eta": 22, "estimate": 22}
    body = (await call(client, world.courier_user, "getOfferRank", ask)).json()
    assert [(e["who"], e["eta_minutes"], e["verified"]) for e in body["board"]] == [
        ("me", 22, True), ("A", 18, False), ("B", 30, False),
    ]  # fmt: skip
    assert not any(e["fastest"] for e in body["board"]) and body["eta_rank"] == 1
    # one rank everywhere (owner 19/10/2026, QA M12): the computed ones first, he is 1st of 3
    assert (body["eta_rank_shown"], body["total"], body["my_verified"]) == (1, 3, True)
    async with SessionLocal() as s:  # A computed now: 25 shown, 25 computed
        await s.execute(
            update(OrderOffer)
            .where(OrderOffer.proposed_fee == Decimal("3"))
            .values(eta_minutes=25, estimated_eta_minutes=25)
        )
        await s.commit()
    body = (await call(client, world.courier_user, "getOfferRank", ask)).json()
    assert [e["who"] for e in body["board"] if e["fastest"]] == ["me"]
    # an unverified offer is never « le + rapide », even alone with a delay
    alone = await world.order()
    await rival(world, factory, alone, 2, "3", 10, 5, est=None)
    blind = (
        await call(
            client, world.courier_user, "getOfferRank", {"order_id": str(alone.id), "fee": 4, "eta": 30}
        )
    ).json()
    assert not any(e["fastest"] for e in blind["board"])


async def test_board_leaves_out_the_couriers_the_customer_blocked(client, world, factory):
    from app.models import UserBlock

    order = await world.order()
    await rival(world, factory, order, 0, "2", 10, 5)
    blocked = await rival(world, factory, order, 1, "1", 5, 4)
    async with SessionLocal() as s:
        s.add(UserBlock(blocker_id=world.customer.id, blocked_id=blocked.user_id))
        await s.commit()
    body = (
        await call(
            client, world.courier_user, "getOfferRank", {"order_id": str(order.id), "fee": 3, "eta": 20}
        )
    ).json()
    assert (body["rank"], body["total"], body["eta_rank"]) == (2, 2, 2)
    assert [(e["who"], e["fee"]) for e in body["board"]] == [("A", 2.0), ("me", 3.0)]
    assert body["other_fees"] == [2.0]


async def test_estimate_fields_are_for_the_offer_customer_only(client, world, factory):
    order = await world.order()
    sent = await call(
        client,
        world.courier_user,
        "createOrderOffer",
        {"order_id": str(order.id), "fee": 4, "eta_minutes": 14},
    )
    offer_id = sent.json()["offer"]["id"]
    seen = await client.get(f"/api/entities/OrderOffer/{offer_id}", headers=auth(world.customer))
    assert seen.json()["eta_minutes"] == 14 and seen.json()["estimated_eta_minutes"] == 12
    other_user = await factory.user(email="other@example.test", profile=False)
    await world.make_courier(other_user, display_name="Other")
    hidden = await client.get(f"/api/entities/OrderOffer/{offer_id}", headers=auth(other_user))
    assert hidden.status_code == 404
    listed = await client.get(
        "/api/entities/OrderOffer",
        params={"q": json.dumps({"order_id": str(order.id)})},
        headers=auth(other_user),
    )
    assert listed.json() == []


async def test_the_customers_list_is_unchanged_for_acceptance(client, world):
    order = await world.order()
    sent = await call(
        client,
        world.courier_user,
        "createOrderOffer",
        {"order_id": str(order.id), "fee": 4, "eta_minutes": 12},
    )
    offer_id = sent.json()["offer"]["id"]
    accepted = await call(
        client, world.customer, "acceptOrderOffer", {"order_id": str(order.id), "offer_id": offer_id}
    )
    assert accepted.status_code == 200, accepted.text
    assert (await reload(OrderOffer, offer_id)).status == "accepted"


# --- punctuality (owner 18/10/2026): his own, never the customer's nor the ranking's -------------


async def test_punctuality_written_at_delivery_and_shown_to_him_only(client, world, factory):
    from app.services import order_transitions as ot

    order = await world.order(status="on_the_way", courier=world.courier, fee="4", purchase="10")
    async with SessionLocal() as s:
        row = await s.get(Order, order.id)
        row.accepted_at, row.eta_minutes = now() - timedelta(minutes=31), 20
        await ot.transition(s, row, "delivered", world.courier_user, "test")
        await s.commit()
    assert (await reload(Order, order.id)).punctuality_late_minutes == 11
    # nine earlier courses: 7 on time (≤ 5 min late), 2 late
    for n, late in enumerate([-3, 0, 2, 5, 1, -10, 4, 9, 30]):
        past = await world.order(status="delivered", courier=world.courier, fee="4", purchase="10")
        async with SessionLocal() as s:
            await s.execute(
                update(Order)
                .where(Order.id == past.id)
                .values(punctuality_late_minutes=late, delivered_at=now() - timedelta(days=1, minutes=n))
            )
            await s.commit()
    body = (await call(client, world.courier_user, "getMyPunctuality")).json()
    assert body == {
        "success": True, "window": 10, "count": 10, "on_time": 7,
        "avg_late_minutes": round((11 + 2 + 5 + 1 + 4 + 9 + 30) / 10), "tolerance_minutes": 5,
    }  # fmt: skip
    # never on the order the customer reads
    seen = await client.get(f"/api/entities/Order/{order.id}", headers=auth(world.customer))
    assert "punctuality_late_minutes" not in seen.json()
    assert (await call(client, world.customer, "getMyPunctuality")).status_code == 403


def test_punctuality_needs_an_acceptance_and_a_delay():
    from datetime import UTC, datetime

    from app.services.order_transitions import punctuality_late_minutes

    t = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
    assert punctuality_late_minutes(None, 20, t) is None
    assert punctuality_late_minutes(t, None, t) is None
    assert punctuality_late_minutes(t - timedelta(minutes=15), 20, t) == -5
    assert punctuality_late_minutes(t - timedelta(days=5), 20, t) == 1440
