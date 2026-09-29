"""createHotDeal, reserveHotDeal, listHotDeals and the ResaleOrder entity: answers, guards,
notifications, realtime and races."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import select, text

from app.config import settings
from app.db import SessionLocal
from app.models import File, HotDeal, NoResponseCase, Order, User
from app.services import hot_deals
from tests.factories import auth
from tests.order_helpers import SOUSSE_SHOP, TUNIS, OrderWorld, notifications, pt, reload, rows


@pytest.fixture
async def world(factory):
    w = await OrderWorld(factory).setup()
    w.buyer = await factory.user(email="buyer@example.test", full_name="Salma", phone_e164="+21698111222")
    w.buyer2 = await factory.user(email="buyer2@example.test", full_name="Nour")
    return w


async def fn(client, user: User, name: str, body: dict[str, Any]) -> httpx.Response:
    return await client.post(f"/api/functions/{name}", json=body, headers=auth(user))


async def rewind(order_id: uuid.UUID, seconds: float) -> None:
    async with SessionLocal() as s:
        await s.execute(
            text(
                "UPDATE no_response_cases SET started_at = started_at - make_interval(secs => :s), "
                "deadline_at = deadline_at - make_interval(secs => :s), "
                "final_at = final_at - make_interval(secs => :s) WHERE order_id = :o"
            ),
            {"s": seconds, "o": order_id},
        )
        await s.commit()


async def parked(client, world, *, wait_over: bool = True, **fields: Any) -> Order:
    """An order whose customer did not answer (reported, deadline past when wait_over)."""
    fields.setdefault("purchase", "25")
    order = await world.order(status="on_the_way", courier=world.courier, fee="5", **fields)
    r = await fn(
        client,
        world.courier_user,
        "triggerEmergencyContact",
        {"order_id": str(order.id), "action": "report_no_response"},
    )
    assert r.status_code == 200
    if wait_over:
        await rewind(order.id, 200)
    return order


async def upload(owner: User, name: str = "deal.jpg") -> str:
    key = f"public/hot_deal/{owner.id}/{name}"
    async with SessionLocal() as s:
        s.add(File(key=key, owner_id=owner.id, visibility="public", content_type="image/jpeg", size_bytes=10))
        await s.commit()
    return f"{settings.public_files_base_url}/{key}"


async def a_deal(world, *, at: tuple[float, float] | None = SOUSSE_SHOP, **fields: Any) -> HotDeal:
    source = await world.order(status="cancelled", courier=world.courier, purchase="20")
    values: dict[str, Any] = {
        "original_order_id": source.id,
        "courier_id": world.courier.id,
        "items_text": "Pain x3",
        "shop_name": "Monoprix",
        "purchase_amount": Decimal("20"),
        "discount_percentage": Decimal("10"),
        "price": Decimal("18"),
        "delivery_fee": Decimal("4"),
        "pickup_location": pt(*at) if at else None,
        "expires_at": datetime.now(UTC) + timedelta(hours=2),
    }
    values.update(fields)
    async with SessionLocal() as s:
        deal = HotDeal(**values)
        s.add(deal)
        await s.commit()
        await s.refresh(deal)
        return deal


async def incidents_of(user: User) -> int:
    async with SessionLocal() as s:
        return (
            await s.execute(
                text("SELECT no_response_incidents FROM customer_stats WHERE user_id = :u"), {"u": user.id}
            )
        ).scalar_one()


# --- createHotDeal -----------------------------------------------------------------------------------


async def test_create_deal_resells_and_closes_the_order(client, world):
    order = await parked(client, world)
    photo = await upload(world.courier_user)
    r = await fn(
        client,
        world.courier_user,
        "createHotDeal",
        {
            "order_id": str(order.id),
            "discount_percentage": 20,
            "delivery_fee": 4,
            "photo_url": photo,
            "lang": "fr",
        },
    )
    assert r.status_code == 200 and r.json()["success"] is True
    deal = await reload(HotDeal, uuid.UUID(r.json()["deal_id"]))
    assert deal.price == Decimal("20.000") and deal.purchase_amount == Decimal("25.000")
    assert deal.discount_percentage == Decimal("20") and deal.delivery_fee == Decimal("4.000")
    assert deal.shop_name == "Monoprix" and deal.status == "available" and deal.pickup_location is not None
    assert timedelta(hours=1, minutes=59) < deal.expires_at - datetime.now(UTC) <= timedelta(hours=2)
    closed = await reload(Order, order.id)
    assert closed.status == "cancelled" and closed.cancelled_by == "courier"
    assert closed.cancel_reason == "client_no_response" and closed.notes == "Remis en vente"
    [case] = await rows(select(NoResponseCase))
    assert case.resolution == "resold" and case.incident_counted is True and case.final_at is not None
    assert await incidents_of(world.customer) == 1
    [told] = await notifications(world.customer, "order_cancelled")
    assert told.data["resale_order_id"] == str(deal.id) and "remis en vente" in told.body_fr
    doc = (await client.get(f"/api/entities/Order/{order.id}", headers=auth(world.customer))).json()
    assert (
        doc["no_response_resolution"] == "resold" and doc["status_history"][-1]["source"] == "createHotDeal"
    )

    public = (await client.get(f"/api/entities/ResaleOrder/{deal.id}", headers=auth(world.buyer))).json()
    assert (
        public["photo_url"] == photo
        and public["discounted_price"] == 20
        and public["courier_name"] == "Karim Trabelsi"
    )
    assert public["courier_phone"] is None and public["courier_lat"] is None and public["buyer_id"] is None
    admin = (await client.get(f"/api/entities/ResaleOrder/{deal.id}", headers=auth(world.admin))).json()
    assert admin["courier_phone"] == "+21655123456" and admin["courier_lat"] == pytest.approx(SOUSSE_SHOP[0])
    assert admin["original_order_id"] == str(order.id) and admin["created_by"] == "courier@example.test"


async def test_create_deal_guards(client, world):
    assert (await fn(client, world.courier_user, "createHotDeal", {})).json() == {"error": "Missing order_id"}
    missing = await fn(client, world.courier_user, "createHotDeal", {"order_id": str(uuid.uuid4())})
    assert missing.status_code == 404 and missing.json() == {"error": "Order not found"}
    order = await parked(client, world)
    for intruder in (world.customer, world.buyer):
        r = await fn(client, intruder, "createHotDeal", {"order_id": str(order.id)})
        assert r.status_code == 403 and r.json() == {"error": "Only the courier of this order can resell it"}

    at_shop = await world.order(status="at_shop", courier=world.courier, purchase="25")
    r = await fn(client, world.courier_user, "createHotDeal", {"order_id": str(at_shop.id)})
    assert r.status_code == 409 and r.json() == {"error": "order_not_resellable", "status": "at_shop"}
    unreported = await world.order(status="on_the_way", courier=world.courier, purchase="25")
    r = await fn(client, world.courier_user, "createHotDeal", {"order_id": str(unreported.id)})
    assert r.status_code == 409 and r.json()["error"] == "order_not_resellable"
    unpaid = await parked(client, world, purchase=None)
    r = await fn(client, world.courier_user, "createHotDeal", {"order_id": str(unpaid.id)})
    assert r.json()["error"] == "order_not_resellable"


async def test_create_deal_waits_for_the_server_deadline(client, world):
    order = await parked(client, world, wait_over=False)
    r = await fn(client, world.courier_user, "createHotDeal", {"order_id": str(order.id)})
    assert r.status_code == 409 and r.json()["error"] == "wait" and r.json()["deadline_at"]
    assert r.json()["status"] == "client_no_response"
    assert await rows(select(HotDeal)) == []


async def test_create_deal_refused_after_the_customer_answered(client, world):
    order = await parked(client, world)
    await fn(
        client,
        world.customer,
        "triggerEmergencyContact",
        {"order_id": str(order.id), "action": "customer_confirms"},
    )
    r = await fn(client, world.courier_user, "createHotDeal", {"order_id": str(order.id)})
    assert r.status_code == 409 and r.json() == {
        "error": "customer_answered",
        "deadline_at": None,
        "status": "on_the_way",
    }


async def test_create_deal_keeps_what_the_refresh_recorded(client, world):
    order = await parked(client, world)
    status = {"order_id": str(order.id), "action": "status"}
    assert (await fn(client, world.customer, "triggerEmergencyContact", status)).json()["stage"] == "expired"
    await rewind(order.id, 4 * 3600)  # the auto-close is due: done by the refresh, then refused
    r = await fn(client, world.courier_user, "createHotDeal", {"order_id": str(order.id)})
    assert r.status_code == 409 and r.json()["error"] == "already_closed"
    assert (await reload(Order, order.id)).status == "cancelled"
    [case] = await rows(select(NoResponseCase))
    assert case.resolution == "auto_closed"


async def test_create_deal_finalizes_the_case_when_nobody_polled(client, world):
    order = await parked(client, world)  # deadline past, the case still 'waiting'
    r = await fn(client, world.courier_user, "createHotDeal", {"order_id": str(order.id)})
    assert r.status_code == 200
    assert len(await notifications(world.courier_user, "customer_no_response_final")) == 1


async def test_create_deal_already_listed(client, world):
    order = await parked(client, world)
    existing = await a_deal(world, original_order_id=order.id)
    r = await fn(client, world.courier_user, "createHotDeal", {"order_id": str(order.id)})
    assert r.status_code == 409 and r.json() == {"error": "already_listed", "deal_id": str(existing.id)}


@pytest.mark.parametrize(
    ("body", "discount", "fee"),
    [
        ({}, "0", "3"),
        ({"discount_percentage": 150, "delivery_fee": 500}, "90", "100"),
        ({"discount_percentage": -5, "delivery_fee": 0}, "0", "0.5"),
        ({"discount_percentage": "abc", "delivery_fee": "abc"}, "0", "3"),
        ({"discount_percentage": None, "delivery_fee": None}, "0", "0.5"),
        ({"discount_percentage": "33.3", "delivery_fee": "2.5"}, "33.3", "2.5"),
    ],
)
async def test_create_deal_bounds(client, world, body, discount, fee):
    order = await parked(client, world)
    r = await fn(
        client, world.courier_user, "createHotDeal", {"order_id": str(order.id), "lang": "ar", **body}
    )
    assert r.status_code == 200
    deal = await reload(HotDeal, uuid.UUID(r.json()["deal_id"]))
    assert deal.discount_percentage == Decimal(discount) and deal.delivery_fee == Decimal(fee)
    expected = (Decimal("25") * (1 - Decimal(discount) / 100)).quantize(Decimal("0.001"))
    assert deal.price == expected
    assert (await reload(Order, order.id)).notes == "تم إعادة عرضه للبيع"


async def test_create_deal_photo_only_the_couriers_own_upload(client, world):
    for photo in ("https://evil.example/x.jpg", await upload(world.customer, "c.jpg"), 42):
        order = await parked(client, world)
        r = await fn(
            client, world.courier_user, "createHotDeal", {"order_id": str(order.id), "photo_url": photo}
        )
        assert r.status_code == 200
        assert (await reload(HotDeal, uuid.UUID(r.json()["deal_id"]))).photo_key is None


async def test_race_resale_against_the_customer_answer(client, world):
    for _ in range(3):
        order = await parked(client, world)
        resale, answer = await asyncio.gather(
            fn(client, world.courier_user, "createHotDeal", {"order_id": str(order.id)}),
            fn(
                client,
                world.customer,
                "triggerEmergencyContact",
                {"order_id": str(order.id), "action": "customer_confirms"},
            ),
        )
        final = await reload(Order, order.id)
        deals = await rows(select(HotDeal).where(HotDeal.original_order_id == order.id))
        if resale.status_code == 200:
            assert answer.status_code == 409 and answer.json()["error"] == "too_late"
            assert final.status == "cancelled" and len(deals) == 1
        else:
            assert answer.status_code == 200 and resale.status_code == 409
            assert resale.json()["error"] == "customer_answered"
            assert final.status == "on_the_way" and deals == []


# --- reserveHotDeal ----------------------------------------------------------------------------------


async def test_reserve_creates_the_buyers_order(client, world):
    deal = await a_deal(world)
    r = await fn(
        client,
        world.buyer,
        "reserveHotDeal",
        {
            "resale_order_id": str(deal.id),
            "delivery_address": "  Rue X  ",
            "delivery_lat": 35.83,
            "delivery_lng": 10.62,
            "phone": "98 765 432",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True and body["resale_order_id"] == str(deal.id)
    assert body["courier_phone"] == "+21655123456"
    order = await reload(Order, uuid.UUID(body["order_id"]))
    assert (
        order.status == "accepted"
        and order.courier_id == world.courier.id
        and order.customer_id == world.buyer.id
    )
    assert order.resale_deal_id == deal.id and order.delivery_address == "Rue X"
    assert order.contact_phone_e164 == "+21698765432" and order.notes == "Hot deal reservation (10% off)"
    doc = (await client.get(f"/api/entities/Order/{order.id}", headers=auth(world.courier_user))).json()
    assert doc["total_amount"] == 22 and doc["purchase_amount"] == 18 and doc["delivery_fee"] == 4
    assert doc["resale_order_id"] == str(deal.id) and doc["shop_name"] == "Monoprix"
    assert doc["status_history"] == [
        {"status": "accepted", "timestamp": doc["status_history"][0]["timestamp"], "source": "reserveHotDeal"}
    ]
    assert doc["delivery_lat"] == pytest.approx(35.83)
    sold = await reload(HotDeal, deal.id)
    assert sold.status == "sold" and sold.buyer_id == world.buyer.id and sold.buyer_order_id == order.id
    [told] = await notifications(world.courier_user, "hot_deal_reserved")
    assert told.order_id == order.id and "Salma" in told.body_fr and "Pain x3" in told.body_fr
    assert told.data == {
        "resale_order_id": str(deal.id),
        "delivery_address": "Rue X",
        "recipient_role": "courier",
    }
    hidden = await client.get(f"/api/entities/ResaleOrder/{deal.id}", headers=auth(world.buyer))
    assert hidden.status_code == 404  # no longer listed
    admin = (await client.get(f"/api/entities/ResaleOrder/{deal.id}", headers=auth(world.admin))).json()
    assert admin["buyer_id"] == "buyer@example.test" and admin["buyer_name"] == "Salma"
    assert admin["buyer_phone"] == "+21698765432" and admin["delivery_address"] == "Rue X"


async def test_reserve_defaults_phone_and_ignores_bad_coordinates(client, world):
    deal = await a_deal(world, shop_name=None)
    r = await fn(
        client,
        world.buyer,
        "reserveHotDeal",
        {
            "resale_order_id": str(deal.id),
            "delivery_address": "Rue Y",
            "delivery_lat": 48.8,
            "delivery_lng": "x",
            "phone": "abc",
        },
    )
    order = await reload(Order, uuid.UUID(r.json()["order_id"]))
    assert order.contact_phone_e164 == "+21698111222" and order.delivery_location is None
    doc = (await client.get(f"/api/entities/Order/{order.id}", headers=auth(world.buyer))).json()
    assert doc["shop_name"] is None and doc["total_amount"] == 22


async def test_reserve_guards(client, world):
    deal = await a_deal(world)
    assert (await fn(client, world.buyer, "reserveHotDeal", {"resale_order_id": str(deal.id)})).json() == {
        "error": "Missing required fields"
    }
    for bad in (str(uuid.uuid4()), "nope"):
        r = await fn(client, world.buyer, "reserveHotDeal", {"resale_order_id": bad, "delivery_address": "x"})
        assert r.status_code == 404 and r.json() == {"error": "Hot deal not found"}
    own = await fn(
        client,
        world.courier_user,
        "reserveHotDeal",
        {"resale_order_id": str(deal.id), "delivery_address": "x"},
    )
    assert own.status_code == 403 and own.json() == {"error": "You cannot reserve your own hot deal"}

    old = await a_deal(world, expires_at=datetime.now(UTC) - timedelta(minutes=1))
    r = await fn(
        client, world.buyer, "reserveHotDeal", {"resale_order_id": str(old.id), "delivery_address": "x"}
    )
    assert r.status_code == 409 and r.json() == {"error": "Hot deal has expired"}
    assert (await reload(HotDeal, old.id)).status == "expired"  # kept despite the 409
    r = await fn(
        client, world.buyer, "reserveHotDeal", {"resale_order_id": str(old.id), "delivery_address": "x"}
    )
    assert r.json() == {"error": "Hot deal is no longer available"}

    free = await a_deal(world, delivery_fee=Decimal("0"))
    r = await fn(
        client, world.buyer, "reserveHotDeal", {"resale_order_id": str(free.id), "delivery_address": "x"}
    )
    assert r.status_code == 500 and r.json() == {"error": "Invalid hot deal delivery fee"}
    assert (await reload(HotDeal, free.id)).status == "available"


async def test_at_most_two_running_reservations(client, world):
    deals = [await a_deal(world) for _ in range(3)]
    for deal in deals[:2]:
        r = await fn(
            client, world.buyer, "reserveHotDeal", {"resale_order_id": str(deal.id), "delivery_address": "x"}
        )
        assert r.status_code == 200
    third = await fn(
        client, world.buyer, "reserveHotDeal", {"resale_order_id": str(deals[2].id), "delivery_address": "x"}
    )
    assert third.status_code == 429 and third.json() == {"error": "too_many_reservations"}
    assert (await reload(HotDeal, deals[2].id)).status == "available"
    # a delivered reservation no longer counts
    first = (await rows(select(Order).where(Order.customer_id == world.buyer.id)))[0]
    async with SessionLocal() as s:
        row = await s.get(Order, first.id)
        row.status, row.delivered_at = "delivered", datetime.now(UTC)
        await s.commit()
    r = await fn(
        client, world.buyer, "reserveHotDeal", {"resale_order_id": str(deals[2].id), "delivery_address": "x"}
    )
    assert r.status_code == 200


async def test_race_two_buyers_one_deal(client, world):
    deal = await a_deal(world)
    answers = await asyncio.gather(
        *(
            fn(client, buyer, "reserveHotDeal", {"resale_order_id": str(deal.id), "delivery_address": "x"})
            for buyer in (world.buyer, world.buyer2)
        )
    )
    assert sorted(r.status_code for r in answers) == [200, 409]
    assert len(await rows(select(Order).where(Order.resale_deal_id == deal.id))) == 1
    assert len(await notifications(world.courier_user, "hot_deal_reserved")) == 1


async def test_race_one_buyer_many_deals(client, world):
    deals = [await a_deal(world) for _ in range(4)]
    answers = await asyncio.gather(
        *(
            fn(client, world.buyer, "reserveHotDeal", {"resale_order_id": str(d.id), "delivery_address": "x"})
            for d in deals
        )
    )
    assert sorted(r.status_code for r in answers) == [200, 200, 429, 429]


async def test_buyer_cancellation_relists_the_deal(client, world):
    deal = await a_deal(world)
    r = await fn(
        client, world.buyer, "reserveHotDeal", {"resale_order_id": str(deal.id), "delivery_address": "x"}
    )
    order_id = r.json()["order_id"]
    c = await fn(
        client,
        world.buyer,
        "cancelOrder",
        {"order_id": order_id, "reason": "changed my mind", "cancelled_by": "customer"},
    )
    assert c.status_code == 200
    relisted = await reload(HotDeal, deal.id)
    assert relisted.status == "available" and relisted.buyer_id is None


# --- listHotDeals ----------------------------------------------------------------------------------


async def test_list_nearest_first_public_fields_only(client, world):
    near = await a_deal(world, at=(35.8260, 10.6090), items_text="near")
    far = await a_deal(world, at=(35.90, 10.70), items_text="far")
    await a_deal(world, at=TUNIS, items_text="tunis")  # ~ 110 km
    nowhere = await a_deal(world, at=None, items_text="nowhere")
    await a_deal(world, expires_at=datetime.now(UTC) - timedelta(seconds=1), items_text="expired")
    await a_deal(world, status="sold", buyer_id=world.buyer.id, items_text="sold")
    r = await fn(
        client, world.buyer, "listHotDeals", {"lat": SOUSSE_SHOP[0], "lng": SOUSSE_SHOP[1], "radius_km": 50}
    )
    body = r.json()
    assert body["success"] is True and body["next_cursor"] is None
    assert [d["items_text"] for d in body["deals"]] == ["near", "far", "nowhere"]
    assert body["total"] == 3
    first = body["deals"][0]
    assert set(first) == {*hot_deals.PUBLIC_FIELDS, "distance_km"}
    assert first["id"] == str(near.id) and first["distance_km"] == 0.1
    assert (
        first["discounted_price"] == 18
        and first["status"] == "available"
        and first["courier_name"] == "Karim T."  # first name + initial (Aurora)
    )
    assert body["deals"][1]["id"] == str(far.id) and body["deals"][1]["distance_km"] > 10
    assert body["deals"][2]["id"] == str(nowhere.id) and body["deals"][2]["distance_km"] is None
    wide = (
        await fn(
            client,
            world.buyer,
            "listHotDeals",
            {"lat": SOUSSE_SHOP[0], "lng": SOUSSE_SHOP[1], "radius_km": 500},
        )
    ).json()
    assert [d["items_text"] for d in wide["deals"]] == ["near", "far", "tunis", "nowhere"]  # capped at 200 km


async def test_list_without_a_point_and_pagination(client, world):
    for i in range(5):
        await a_deal(world, items_text=f"d{i}")
    r = (await fn(client, world.buyer, "listHotDeals", {"lat": "35.8", "lng": 10.6, "limit": 2})).json()
    assert [d["items_text"] for d in r["deals"]] == ["d4", "d3"] and r["next_cursor"] == "2"
    assert all(d["distance_km"] is None for d in r["deals"])
    page = (await fn(client, world.buyer, "listHotDeals", {"limit": 2, "cursor": r["next_cursor"]})).json()
    assert [d["items_text"] for d in page["deals"]] == ["d2", "d1"] and page["next_cursor"] == "4"
    last = (await fn(client, world.buyer, "listHotDeals", {"limit": 2, "cursor": "4"})).json()
    assert [d["items_text"] for d in last["deals"]] == ["d0"] and last["next_cursor"] is None
    capped = (await fn(client, world.buyer, "listHotDeals", {"limit": 500, "cursor": "x"})).json()
    assert capped["total"] == 5
    zero = (await fn(client, world.buyer, "listHotDeals", {"limit": 0, "cursor": -3})).json()
    assert zero["total"] == 5  # 0 → default 30
    empty_radius = (
        await fn(client, world.buyer, "listHotDeals", {"lat": 35.9, "lng": 10.8, "radius_km": None})
    ).json()
    assert empty_radius["deals"] == []  # Number(null) = 0 km


async def test_list_one_deal_by_id_for_the_detail_page(client, world):
    far = await a_deal(world, at=TUNIS, items_text="tunis")  # beyond the default radius
    sold = await a_deal(world, status="sold", buyer_id=world.buyer.id, items_text="sold")
    where = {"lat": SOUSSE_SHOP[0], "lng": SOUSSE_SHOP[1]}
    one = (await fn(client, world.buyer, "listHotDeals", {"id": str(far.id), **where})).json()
    assert [d["id"] for d in one["deals"]] == [str(far.id)] and one["deals"][0]["distance_km"] > 100
    no_point = (await fn(client, world.buyer, "listHotDeals", {"id": str(far.id)})).json()
    assert no_point["deals"][0]["distance_km"] is None
    for gone in (str(sold.id), "not-a-uuid", "00000000-0000-0000-0000-000000000000"):
        assert (await fn(client, world.buyer, "listHotDeals", {"id": gone})).json()["deals"] == []


# --- ResaleOrder entity and realtime -----------------------------------------------------------------


async def test_resale_order_entity_rules(client, world):
    listed = await a_deal(world)
    sold = await a_deal(world, status="sold", buyer_id=world.buyer.id)
    docs = (await client.get("/api/entities/ResaleOrder", headers=auth(world.buyer2))).json()
    assert [d["id"] for d in docs] == [str(listed.id)]
    assert (
        await client.get(f"/api/entities/ResaleOrder/{sold.id}", headers=auth(world.buyer))
    ).status_code == 404
    everything = (await client.get("/api/entities/ResaleOrder", headers=auth(world.admin))).json()
    assert {d["id"] for d in everything} == {str(listed.id), str(sold.id)}
    probe = await client.get(
        "/api/entities/ResaleOrder",
        params={"q": '{"courier_phone":"+21655123456"}'},
        headers=auth(world.buyer2),
    )
    assert probe.json() == []  # a private field can't be probed
    url = f"/api/entities/ResaleOrder/{listed.id}"
    assert (await client.patch(url, json={"status": "sold"}, headers=auth(world.admin))).status_code == 403
    assert (await client.delete(url, headers=auth(world.courier_user))).status_code == 403
    assert (
        await client.post("/api/entities/ResaleOrder", json={}, headers=auth(world.courier_user))
    ).status_code == 403


async def test_realtime_a_deal_leaving_the_listing_is_announced_as_delete(client, world, monkeypatch):
    seen: list[tuple[str, str]] = []
    original = hot_deals.emit

    def record(session, entity, type_, id_, audience=None):
        seen.append((entity, type_))
        original(session, entity, type_, id_, audience)

    monkeypatch.setattr(hot_deals, "emit", record)
    order = await parked(client, world)
    r = await fn(client, world.courier_user, "createHotDeal", {"order_id": str(order.id)})
    assert ("ResaleOrder", "create") in seen
    seen.clear()
    await fn(
        client,
        world.buyer,
        "reserveHotDeal",
        {"resale_order_id": r.json()["deal_id"], "delivery_address": "x"},
    )
    assert seen == [("ResaleOrder", "delete")]
