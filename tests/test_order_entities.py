"""Order, OrderOffer and CourierProfile compat entities: shape, read policies, field guards, writes."""

import json
import re
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import (
    Courier,
    CourierLedgerEntry,
    File,
    NoResponseCase,
    OrderIssue,
    OrderOffer,
    OrderRating,
    OrderStop,
)
from tests.factories import auth, error_of
from tests.order_helpers import SOUSSE_SHOP, OrderWorld, notifications, now, reload, set_live

ISO_NAIVE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}$")


@pytest.fixture
async def world(factory):
    return await OrderWorld(factory).setup()


async def get_orders(client, user, q=None, **params):
    if q is not None:
        params["q"] = json.dumps(q)
    response = await client.get("/api/entities/Order", params=params, headers=auth(user))
    assert response.status_code == 200, response.text
    return response.json()


async def test_order_legacy_shape_for_the_customer(client, world):
    order = await world.order(status="on_the_way", courier=world.courier, fee="7", purchase="23.5", stops=2)
    await set_live(order, world.courier, 35.82, 10.62)
    async with SessionLocal() as s:
        s.add(
            CourierLedgerEntry(
                courier_id=world.courier.id,
                order_id=order.id,
                kind="commission_waived_launch",
                amount=Decimal("0.5"),
            )
        )
        s.add(
            OrderIssue(
                order_id=order.id, reporter_id=world.courier_user.id, issue_type="other", description="x"
            )
        )
        s.add(
            NoResponseCase(
                order_id=order.id,
                courier_id=world.courier.id,
                status="waiting",
                started_at=now(),
                deadline_at=now(),
                channels={"in_app": True},
            )
        )
        await s.commit()
    [doc] = await get_orders(client, world.customer)
    assert doc["id"] == str(order.id)
    assert ISO_NAIVE.match(doc["created_date"]) and ISO_NAIVE.match(doc["updated_date"])
    assert doc["customer_id"] == "cust@example.test" and doc["customer_phone"] == "+21622111222"
    assert doc["courier_id"] == str(world.courier.id)
    assert doc["courier_user_id"] == "courier@example.test"
    assert doc["courier_name"] == "Karim Trabelsi" and doc["courier_phone"] == "+21655123456"
    assert doc["courier_live_lat"] == pytest.approx(35.82) and doc["courier_live_at"]
    assert doc["shop_name"] == "Monoprix" and doc["shop_lat"] == pytest.approx(SOUSSE_SHOP[0])
    assert [s["name"] for s in doc["shops"]] == ["Monoprix", "Shop 2"]
    assert doc["shops"][0]["status"] == "pending" and doc["shops"][0]["lat"] == pytest.approx(SOUSSE_SHOP[0])
    assert doc["delivery_fee"] == 7 and doc["purchase_amount"] == 23.5 and doc["total_amount"] == 30.5
    assert doc["status"] == "on_the_way" and doc["preferred_time"] == "asap"
    assert doc["payment_status"] == "pending" and doc["courier_photo"] is None
    assert [h["status"] for h in doc["status_history"]] == ["pending", "on_the_way"]
    assert doc["status_history"][0]["timestamp"].endswith("Z")
    assert doc["ods_commission"] == 0.5 and doc["ods_commission_status"] == "offered_launch"
    assert doc["platform_fee"] is None  # set at delivery only
    assert doc["has_issues"] is True and doc["reported_issues"][0]["reported_by"] == "courier@example.test"
    assert doc["reported_issues"][0]["courier_id"] == str(world.courier.id)
    assert doc["no_response_reported"] is True and doc["no_response_channels"] == {"in_app": True}
    assert doc["customer_responded_to_emergency"] is False
    assert doc["delivery_details"] == "3e étage"


async def test_delivered_order_commission_and_rating_fields(client, world):
    order = await world.order(status="delivered", courier=world.courier, fee="6", purchase="10")
    async with SessionLocal() as s:
        s.add(
            OrderRating(
                order_id=order.id,
                courier_id=world.courier.id,
                rater_id=world.customer.id,
                rating=4,
                comment="ok",
            )
        )
        s.add(
            CourierLedgerEntry(
                courier_id=world.courier.id, order_id=order.id, kind="commission_due", amount=Decimal("0.5")
            )
        )
        await s.commit()
    [doc] = await get_orders(client, world.courier_user, {"courier_id": str(world.courier.id)})
    assert doc["platform_fee"] == 0 and doc["courier_net_earning"] == 6
    assert doc["ods_commission_status"] == "due"
    assert doc["customer_rating"] == 4 and doc["rating_comment"] == "ok"
    assert doc["courier_stats_recorded_at"] == doc["delivered_at"]


async def test_open_orders_readable_by_verified_couriers_without_private_fields(client, world, factory):
    mine = await world.order()
    other_customer = await factory.user(email="other@example.test")
    taken = await world.order(other_customer, status="accepted", courier=world.courier)
    pending_user = await factory.user(email="newbie@example.test", profile=False)
    await world.make_courier(pending_user, verification="pending")
    stranger = await factory.user(email="stranger@example.test")
    await set_live(taken, world.courier, 35.8, 10.6)

    other_courier_user = await factory.user(email="c2@example.test", profile=False)
    await world.make_courier(other_courier_user)
    docs = await get_orders(client, other_courier_user)
    assert [d["id"] for d in docs] == [str(mine.id)]
    assert docs[0]["customer_phone"] is None and docs[0]["delivery_details"] is None
    assert docs[0]["reported_issues"] is None and docs[0]["has_issues"] is None
    # a hidden field can't be probed through a filter
    assert await get_orders(client, other_courier_user, {"customer_phone": "+21622111222"}) == []

    assert await get_orders(client, pending_user) == []
    assert await get_orders(client, stranger) == []
    missing = await client.get(f"/api/entities/Order/{taken.id}", headers=auth(stranger))
    assert missing.status_code == 404
    courier_view = (
        await client.get(f"/api/entities/Order/{taken.id}", headers=auth(world.courier_user))
    ).json()
    assert courier_view["customer_phone"] and courier_view["courier_live_lat"] == pytest.approx(35.8)
    admin_docs = await get_orders(client, world.admin, sort="-created_date")
    assert {d["id"] for d in admin_docs} == {str(mine.id), str(taken.id)}


async def test_qa_orders_only_for_qa_accounts(client, world, factory):
    await world.order(items="QA TEST pain")
    await world.order(items="PW-123 lait")
    real = await world.order(items="Lait")
    qa_user = await factory.user(email="vovine2891@sepole.com", profile=False)
    await world.make_courier(qa_user)
    assert [d["id"] for d in await get_orders(client, world.courier_user)] == [str(real.id)]
    assert len(await get_orders(client, qa_user)) == 3
    assert len(await get_orders(client, world.customer)) == 3  # his own, whatever the items


async def test_filters_used_by_the_courier_screens(client, world):
    active = await world.order(status="at_shop", courier=world.courier)
    await world.order(status="delivered", courier=world.courier, fee="5")
    docs = await get_orders(
        client,
        world.courier_user,
        {"courier_id": str(world.courier.id), "status": {"$in": ["accepted", "at_shop"]}},
        sort="-updated_date",
    )
    assert [d["id"] for d in docs] == [str(active.id)]
    by_email = await get_orders(client, world.admin, {"courier_user_id": "courier@example.test"})
    assert len(by_email) == 2


async def test_order_writes_refused_outside_the_policies(client, world, factory):
    order = await world.order()
    created = await client.post("/api/entities/Order", json={"items_text": "x"}, headers=auth(world.customer))
    assert created.status_code == 403 and error_of(created) == "permission_denied"
    deleted = await client.delete(f"/api/entities/Order/{order.id}", headers=auth(world.customer))
    assert deleted.status_code == 403
    # OrderOffers.jsx fallback: the customer promoting his own order
    promoted = await client.patch(
        f"/api/entities/Order/{order.id}",
        json={"status": "offers_received", "status_history": []},
        headers=auth(world.customer),
    )
    assert promoted.status_code == 403
    # OrderTracking fallback of rateCourier
    delivered = await world.order(status="delivered", courier=world.courier, fee="5")
    rated = await client.patch(
        f"/api/entities/Order/{delivered.id}", json={"customer_rating": 5}, headers=auth(world.customer)
    )
    assert rated.status_code == 403
    # a verified courier who is not assigned
    bidder = await factory.user(email="bidder@example.test", profile=False)
    await world.make_courier(bidder)
    hijack = await client.patch(
        f"/api/entities/Order/{order.id}", json={"status": "accepted"}, headers=auth(bidder)
    )
    assert hijack.status_code == 403
    admin = await client.patch(
        f"/api/entities/Order/{order.id}", json={"status": "cancelled"}, headers=auth(world.admin)
    )
    assert admin.status_code == 403
    stranger = await factory.user(email="s@example.test")
    unseen = await client.patch(f"/api/entities/Order/{order.id}", json={}, headers=auth(stranger))
    assert unseen.status_code == 404
    for entity in ("OrderOffer", "CourierProfile"):
        denied = await client.post(f"/api/entities/{entity}", json={}, headers=auth(world.admin))
        assert denied.status_code == 403


async def test_customer_geocode_only_fills_an_empty_delivery_point(client, world):
    order = await world.order(delivery=None)
    body = {"shop_lat": 36.0, "shop_lng": 10.0, "delivery_lat": 35.9, "delivery_lng": 10.5}
    done = await client.patch(f"/api/entities/Order/{order.id}", json=body, headers=auth(world.customer))
    assert done.status_code == 200
    assert done.json()["delivery_lat"] == pytest.approx(35.9)
    assert done.json()["shop_lat"] == pytest.approx(SOUSSE_SHOP[0])  # shop_* ignored
    again = await client.patch(
        f"/api/entities/Order/{order.id}",
        json={"delivery_lat": 36.5, "delivery_lng": 10.1},
        headers=auth(world.customer),
    )
    assert again.json()["delivery_lat"] == pytest.approx(35.9)  # never overwritten
    outside = await world.order(delivery=None)
    paris = await client.patch(
        f"/api/entities/Order/{outside.id}",
        json={"delivery_lat": 48.8, "delivery_lng": 2.3},
        headers=auth(world.customer),
    )
    assert paris.json()["delivery_lat"] is None


# --- OrderOffer -------------------------------------------------------------------------------------


async def test_offer_reads_and_withdrawal(client, world, factory):
    order = await world.order(status="offers_received")
    offer = await world.offer(order)
    other_user = await factory.user(email="c2@example.test", profile=False)
    other = await world.make_courier(other_user)
    other_offer = await world.offer(order, other, fee="6")

    def ids(response):
        assert response.status_code == 200
        return sorted(d["id"] for d in response.json())

    customer_view = await client.get(
        "/api/entities/OrderOffer",
        params={"q": json.dumps({"order_id": str(order.id)})},
        headers=auth(world.customer),
    )
    assert ids(customer_view) == sorted([str(offer.id), str(other_offer.id)])
    doc = next(d for d in customer_view.json() if d["id"] == str(offer.id))
    assert doc["customer_id"] == "cust@example.test" and doc["courier_user_id"] == "courier@example.test"
    assert doc["courier_name"] == "Karim Trabelsi" and doc["courier_vehicle"] == "scooter"
    assert (
        doc["proposed_fee"] == 5 and doc["created_via"] == "createOrderOffer" and doc["courier_photo"] is None
    )
    assert ids(await client.get("/api/entities/OrderOffer", headers=auth(world.courier_user))) == [
        str(offer.id)
    ]

    # only the offer's courier deletes (withdraws) it
    refused = await client.delete(f"/api/entities/OrderOffer/{offer.id}", headers=auth(world.customer))
    assert refused.status_code == 403
    hidden = await client.delete(f"/api/entities/OrderOffer/{offer.id}", headers=auth(other_user))
    assert hidden.status_code == 404
    gone = await client.delete(f"/api/entities/OrderOffer/{offer.id}", headers=auth(world.courier_user))
    assert gone.status_code == 200
    assert (await reload(OrderOffer, offer.id)).status == "withdrawn"
    assert ids(await client.get("/api/entities/OrderOffer", headers=auth(world.courier_user))) == []
    assert (
        await client.delete(f"/api/entities/OrderOffer/{offer.id}", headers=auth(world.courier_user))
    ).status_code == 404
    order_doc = (await client.get(f"/api/entities/Order/{order.id}", headers=auth(world.customer))).json()
    assert order_doc["status"] == "offers_received"  # another offer is still pending

    await client.delete(f"/api/entities/OrderOffer/{other_offer.id}", headers=auth(other_user))
    order_doc = (await client.get(f"/api/entities/Order/{order.id}", headers=auth(world.customer))).json()
    assert order_doc["status"] == "pending"
    assert order_doc["status_history"][-1]["source"] == "offer_withdrawn"
    bad = await client.delete("/api/entities/OrderOffer/not-a-uuid", headers=auth(world.courier_user))
    assert bad.status_code == 404


async def test_accepted_offer_cannot_be_deleted(client, world):
    order = await world.order(status="accepted", courier=world.courier)
    offer = await world.offer(order, status="accepted")
    response = await client.delete(f"/api/entities/OrderOffer/{offer.id}", headers=auth(world.courier_user))
    assert response.status_code == 403


# --- CourierProfile ---------------------------------------------------------------------------------


async def test_courier_profile_reads(client, world, factory):
    async with SessionLocal() as s:
        courier = await s.get(Courier, world.courier.id)
        courier.id_document_key = "private/generic/x/secret.jpg"
        courier.service_start = __import__("datetime").time(8, 30)
        await s.commit()
    await world.order(status="delivered", courier=world.courier, fee="4.5")
    own = await client.get(
        "/api/entities/CourierProfile",
        params={"q": json.dumps({"user_id": "courier@example.test"})},
        headers=auth(world.courier_user),
    )
    [doc] = own.json()
    assert "id_photo_uri" not in doc and "secret" not in json.dumps(doc)
    assert doc["full_name"] == "Karim Trabelsi" and doc["verification_status"] == "verified"
    assert doc["total_deliveries"] == 1 and doc["total_earnings"] == 4.5 and doc["average_rating"] == 5
    assert doc["current_lat"] == pytest.approx(SOUSSE_SHOP[0]) and doc["service_start_time"] == "08:30"
    probe = await client.get(
        "/api/entities/CourierProfile",
        params={"q": json.dumps({"id_photo_uri": "x"})},
        headers=auth(world.admin),
    )
    assert probe.status_code == 400
    stranger = await factory.user(email="s@example.test")
    assert (await client.get("/api/entities/CourierProfile", headers=auth(stranger))).json() == []
    assert len((await client.get("/api/entities/CourierProfile", headers=auth(world.admin))).json()) == 1


async def test_admin_verification(client, world, factory):
    user = await factory.user(email="new@example.test", profile=False)
    courier = await world.make_courier(user, verification="pending")
    own = await client.patch(
        f"/api/entities/CourierProfile/{courier.id}",
        json={"verification_status": "verified"},
        headers=auth(user),
    )
    assert own.status_code == 403
    done = await client.patch(
        f"/api/entities/CourierProfile/{courier.id}",
        json={"verification_status": "verified", "is_online": False, "total_deliveries": 99},
        headers=auth(world.admin),
    )
    assert done.status_code == 200 and done.json()["verification_status"] == "verified"
    assert (
        done.json()["is_online"] is True and done.json()["total_deliveries"] == 0
    )  # not the admin's to write
    row = await reload(Courier, courier.id)
    assert row.verified_by == world.admin.id and row.verified_at is not None
    # AdminDashboard writes the courier's notice itself (Notification.create): none from here
    assert await notifications(user) == []
    same = await client.patch(
        f"/api/entities/CourierProfile/{courier.id}",
        json={"verification_status": "verified"},
        headers=auth(world.admin),
    )
    assert same.status_code == 200 and (await reload(Courier, courier.id)).verified_at == row.verified_at
    rejected = await client.patch(
        f"/api/entities/CourierProfile/{courier.id}",
        json={"verification_status": "rejected"},
        headers=auth(world.admin),
    )
    assert rejected.json()["verification_status"] == "rejected"
    back = await client.patch(
        f"/api/entities/CourierProfile/{courier.id}",
        json={"verification_status": "pending"},
        headers=auth(world.admin),
    )
    assert back.status_code == 200 and (await reload(Courier, courier.id)).verified_at is None
    wrong = await client.patch(
        f"/api/entities/CourierProfile/{courier.id}",
        json={"verification_status": "maybe"},
        headers=auth(world.admin),
    )
    assert wrong.status_code == 400
    assert (
        await client.patch("/api/entities/CourierProfile/nope", json={}, headers=auth(world.admin))
    ).status_code == 404
    unknown = "00000000-0000-0000-0000-000000000000"
    assert (
        await client.patch(f"/api/entities/CourierProfile/{unknown}", json={}, headers=auth(world.admin))
    ).status_code == 404


async def test_realtime_order_event_reaches_the_open_order_bidders(world):
    """The hub reads an Order event through the read policy: verified couriers get open orders."""
    from app.compat.query import get_document
    from app.compat.registry import get_entity
    from app.security.deps import to_current_user

    order = await world.order()
    async with SessionLocal() as s:
        doc = await get_document(s, get_entity("Order"), to_current_user(world.courier_user), str(order.id))
    assert doc is not None and doc["customer_phone"] is None


async def test_stop_receipt_is_answered_as_public_url(client, world):
    order = await world.order(status="purchased", courier=world.courier, purchase="12")
    async with SessionLocal() as s:
        stop = (await s.execute(select(OrderStop).where(OrderStop.order_id == order.id))).scalar_one()
        stop.receipt_key, stop.status = "public/receipt/2026/09/r.jpg", "purchased"
        s.add(
            File(
                key="public/receipt/2026/09/r.jpg",
                owner_id=world.courier_user.id,
                visibility="public",
                content_type="image/jpeg",
                size_bytes=3,
            )
        )
        await s.commit()
    doc = (await client.get(f"/api/entities/Order/{order.id}", headers=auth(world.customer))).json()
    assert doc["receipt_photo_url"].endswith("/public/receipt/2026/09/r.jpg")
    assert doc["shops"][0]["receipt_photo_url"] == doc["receipt_photo_url"]
