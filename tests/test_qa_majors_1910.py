"""QA campaign 18-19/10/2026, the major findings fixed server side (M3 … M15, rapport technique)."""

import uuid

import pytest
from sqlalchemy import select

from app.models import Courier, Order
from tests.factories import auth
from tests.order_helpers import SOUSSE_HOME, SOUSSE_SHOP, OrderWorld, device, notifications, rows


@pytest.fixture
async def world(factory):
    return await OrderWorld(factory).setup()


async def call(client, user, name, payload=None):
    return await client.post(f"/api/functions/{name}", json=payload or {}, headers=auth(user))


def order_form(**overrides):
    form = {
        "items_text": "2x Pain\n1x Lait",
        "shop_name": "Monoprix",
        "shop_address": "Av. Habib Bourguiba",
        "shop_governorate": "Sousse",
        "shop_lat": SOUSSE_SHOP[0],
        "shop_lng": SOUSSE_SHOP[1],
        "delivery_address": "Rue de la plage",
        "delivery_governorate": "Sousse",
        "delivery_lat": SOUSSE_HOME[0],
        "delivery_lng": SOUSSE_HOME[1],
        "preferred_time": "asap",
    }
    form.update(overrides)
    return form


async def count_orders(customer) -> int:
    return len(await rows(select(Order.id).where(Order.customer_id == customer.id)))


# --- M3: a delivered order sent again after a lost answer is never a duplicate -------------------------


async def test_m3_same_client_ref_answers_the_same_delivery_order(client, world, factory):
    await device(world.courier_user)
    ref = uuid.uuid4().hex
    first = await call(client, world.customer, "placeOrder", {"order": order_form(client_ref=ref)})
    assert first.status_code == 200, first.text
    assert first.json()["dispatched"] == 1 and "existing" not in first.json()
    # the answer was lost: the app sends the same attempt again
    again = await call(client, world.customer, "placeOrder", {"order": order_form(client_ref=ref)})
    assert again.status_code == 200, again.text
    body = again.json()
    assert body["order"]["id"] == first.json()["order"]["id"]
    assert body["existing"] is True and body["dispatched"] == 0
    assert await count_orders(world.customer) == 1
    assert len(await notifications(world.courier_user, "new_order")) == 1  # never broadcast twice

    # a new attempt (new key) is a new order; no key at all: every call is a new order (older apps)
    other = await call(
        client, world.customer, "placeOrder", {"order": order_form(client_ref=uuid.uuid4().hex)}
    )
    assert other.json()["order"]["id"] != first.json()["order"]["id"]
    await call(client, world.customer, "placeOrder", {"order": order_form()})
    await call(client, world.customer, "placeOrder", {"order": order_form()})
    assert await count_orders(world.customer) == 4

    # the same key from another customer never reaches the first one's order
    someone = await factory.user(email="other@example.test", full_name="Sonia", phone_e164="+21622999000")
    theirs = await call(client, someone, "placeOrder", {"order": order_form(client_ref=ref)})
    assert theirs.status_code == 200 and theirs.json()["order"]["id"] != first.json()["order"]["id"]


async def test_m3_vitrine_delivery_retry_is_idempotent(client, world):
    from tests.test_vitrine_orders import public_vitrine
    from tests.vitrine_helpers import cart_order

    shop_id, [cafe, _] = await public_vitrine(client, world.admin)
    ref = "cart-" + uuid.uuid4().hex[:12]
    body = cart_order(shop_id, [{"product_id": cafe, "qty": 2}], vitrine={"client_ref": ref})
    first = await call(client, world.customer, "placeOrder", body)
    assert first.status_code == 200, first.text
    again = await call(client, world.customer, "placeOrder", body)
    assert again.status_code == 200 and again.json()["existing"] is True
    assert again.json()["order"]["id"] == first.json()["order"]["id"]
    assert await count_orders(world.customer) == 1


# --- M4: cancelling during « article indisponible » is a late cancellation -------------------------------


async def stops_at(order, status):
    from sqlalchemy import update

    from app.db import SessionLocal
    from app.models import OrderStop

    async with SessionLocal() as s:
        await s.execute(update(OrderStop).where(OrderStop.order_id == order.id).values(status=status))
        await s.commit()


async def courier_cancel(client, world, order, reason="vehicle_issue"):
    return await call(
        client,
        world.courier_user,
        "cancelOrder",
        {
            "order_id": str(order.id),
            "cancelled_by": "courier",
            "courier_id": str(world.courier.id),
            "reason": reason,
        },
    )


async def late_count(world) -> int:
    from tests.order_helpers import reload

    return (await reload(Courier, world.courier.id)).late_cancellations


async def test_m4_price_confirmation_needed_counts_as_late(client, world):
    order = await world.order(status="price_confirmation_needed", courier=world.courier, fee="4")
    await stops_at(order, "at_shop")
    policy = await call(
        client, world.courier_user, "getCancellationPolicy", {"order_id": str(order.id), "actor": "courier"}
    )
    assert policy.json()["policy"]["penalty_applies"] is True
    assert policy.json()["policy"]["reason_code"] == "courier_late_cancel_penalty"
    before = await late_count(world)
    r = await courier_cancel(client, world, order)
    assert r.status_code == 200, r.text
    assert await late_count(world) == before + 1


async def test_m4_exceptions_are_kept(client, world):
    before = await late_count(world)
    # « magasin fermé » during the question: no penalty (owner 06/10)
    closed = await world.order(status="price_confirmation_needed", courier=world.courier, fee="4")
    await stops_at(closed, "at_shop")
    assert (await courier_cancel(client, world, closed, "shop_closed")).status_code == 200
    # a Vitrine restaurant's « plat manquant » before he reached the shop: he was not at the shop
    early = await world.order(status="price_confirmation_needed", courier=world.courier, fee="4")
    policy = await call(
        client, world.courier_user, "getCancellationPolicy", {"order_id": str(early.id), "actor": "courier"}
    )
    assert policy.json()["policy"]["penalty_applies"] is False
    assert (await courier_cancel(client, world, early)).status_code == 200
    assert await late_count(world) == before


# --- M6: after two alerts the customer answered, the policy never offers « revendez » ------------------


async def answered_twice(order, world):
    from datetime import timedelta

    from app.db import SessionLocal
    from app.models import NoResponseCase
    from tests.order_helpers import now

    async with SessionLocal() as s:
        for n in range(2):
            start = now() - timedelta(minutes=20 - 5 * n)
            s.add(
                NoResponseCase(
                    order_id=order.id,
                    courier_id=world.courier.id,
                    status="resolved",
                    started_at=start,
                    deadline_at=start + timedelta(minutes=3),
                    resolved_at=start + timedelta(minutes=1),
                    resolution="customer_confirmed",
                )
            )
        await s.commit()


async def test_m6_policy_says_what_he_can_really_do(client, world):
    order = await world.order(status="on_the_way", courier=world.courier, fee="4", purchase="8.4")
    ask = {"order_id": str(order.id), "actor": "courier"}
    # bought, no alert at all: his own choice, the resale is possible (unchanged)
    free = (await call(client, world.courier_user, "getCancellationPolicy", ask)).json()["policy"]
    assert free["can_resell"] is True and "revendez-les" in free["message_fr"]

    await answered_twice(order, world)
    policy = (await call(client, world.courier_user, "getCancellationPolicy", ask)).json()["policy"]
    assert policy["after_purchase"] is True and policy["can_resell"] is False
    assert policy["resale_refusal"] == "customer_answered"
    assert "revend" not in policy["message_fr"].lower()
    assert "terminez la livraison" in policy["message_fr"] and "rendez les articles" in policy["message_fr"]
    # the server agrees: the resale is refused, the return to the shop (+1 late) is the way out
    refused = await call(
        client, world.courier_user, "createHotDeal", {"order_id": str(order.id), "courier_choice": True}
    )
    assert refused.status_code == 409
    back = await courier_cancel(client, world, order, "returned_to_shop")
    assert back.status_code == 200, back.text


# --- M7: an Offre Chaude order never goes to the shop nor has its price rewritten -----------------------


async def test_m7_hot_deal_order_refuses_shop_steps_and_keeps_its_price(client, world):
    from datetime import timedelta
    from decimal import Decimal

    from app.db import SessionLocal
    from app.models import HotDeal
    from tests.order_helpers import now, reload

    source = await world.order(status="cancelled", courier=world.courier, fee="5")
    async with SessionLocal() as s:
        deal = HotDeal(
            original_order_id=source.id,
            courier_id=world.courier.id,
            items_text="x",
            purchase_amount=Decimal("10"),
            discount_percentage=Decimal("16"),
            price=Decimal("8.4"),
            expires_at=now() + timedelta(hours=2),
            status="sold",
            buyer_id=world.customer.id,
        )
        s.add(deal)
        await s.commit()
    order = await world.order(
        status="accepted", courier=world.courier, fee="3", purchase="8.4", resale_deal_id=deal.id
    )
    await stops_at(order, "purchased")

    async def patch(body):
        return await client.patch(
            f"/api/entities/Order/{order.id}", json=body, headers=auth(world.courier_user)
        )

    for status in ("at_shop", "price_confirmation_needed", "purchased"):
        r = await patch({"status": status, "purchase_amount": 9})
        assert r.status_code == 403, (status, r.text)
    fresh = await reload(Order, order.id)
    assert fresh.status == "accepted" and fresh.purchase_amount == Decimal("8.400")
    # the real path still works, the price stays the reserved one
    assert (await patch({"status": "on_the_way"})).status_code == 200
    assert (await patch({"status": "delivered"})).status_code == 200
    assert (await reload(Order, order.id)).purchase_amount == Decimal("8.400")


# --- M8: the decay is announced, the reserved notice says the price obtained ---------------------------


async def test_m8_decay_plan_preview_and_reserved_price(client, world, factory):
    from app.models import HotDeal
    from tests.order_helpers import notifications, reload

    order = await world.order(status="on_the_way", courier=world.courier, fee="4", purchase="10")
    ask = {"order_id": str(order.id), "courier_choice": True}
    preview = await call(client, world.courier_user, "createHotDeal", {**ask, "preview": True})
    assert preview.status_code == 200, preview.text
    plan = preview.json()
    assert plan["preview"] is True and "deal_id" not in plan
    assert (plan["start_price"], plan["floor_price"], plan["drop_step"], plan["drop_every_min"]) == (
        10,
        8,
        0.5,
        5,
    )
    assert [p["price"] for p in plan["price_schedule"]] == [10, 9.5, 9, 8.5, 8]
    assert plan["price_schedule"][-1]["after_min"] == plan["floor_after_min"] == 20
    assert (await reload(Order, order.id)).status == "on_the_way"  # nothing created
    assert await rows(select(HotDeal.id).where(HotDeal.original_order_id == order.id)) == []

    made = await call(client, world.courier_user, "createHotDeal", ask)
    assert made.status_code == 200, made.text
    assert made.json()["price_schedule"] == plan["price_schedule"] and made.json()["expires_at"]

    buyer = await factory.user(email="buyer2@example.test", full_name="Salma", phone_e164="+21698000111")
    r = await call(
        client,
        buyer,
        "reserveHotDeal",
        {"resale_order_id": made.json()["deal_id"], "delivery_address": "Rue X"},
    )
    assert r.status_code == 200, r.text
    [told] = await notifications(world.courier_user, "hot_deal_reserved")
    assert "à 10.000 DT" in told.body_fr and told.data["price"] == 10


# --- M9: a courier the customer blocked can't change his offer, the customer hears nothing -------------


async def test_m9_blocked_courier_cannot_update_his_offer(client, world):
    from decimal import Decimal

    from app.db import SessionLocal
    from app.models import OrderOffer, UserBlock
    from tests.order_helpers import notifications, pushes, reload

    order = await world.order(status="offers_received")
    offer = await world.offer(order, fee="5")
    async with SessionLocal() as s:
        s.add(UserBlock(blocker_id=world.customer.id, blocked_id=world.courier_user.id))
        await s.commit()
    r = await call(client, world.courier_user, "updateOrderOffer", {"offer_id": str(offer.id), "fee": 4})
    assert r.status_code == 409 and r.json()["error"] == "blocked"
    assert (await reload(OrderOffer, offer.id)).proposed_fee == Decimal("5")
    assert await notifications(world.customer, "new_offer") == []
    assert await pushes(world.customer) == []


# --- M10: « Tout lire » marks every unread notification, not only the 50 loaded -------------------------


async def test_m10_mark_all_read_reaches_every_row(client, world, monkeypatch):
    from app.db import SessionLocal
    from app.models import Notification
    from app.services import notifications as notif_service

    seen = []
    original = notif_service.emit

    def record(session, entity, type_, id_, audience=None, data=None):
        seen.append((entity, type_, data))
        original(session, entity, type_, id_, audience, data)

    monkeypatch.setattr(notif_service, "emit", record)
    async with SessionLocal() as s:
        for n in range(130):
            s.add(
                Notification(user_id=world.courier_user.id, type="new_order", title_fr=f"n{n}", body_fr="x")
            )
        s.add(Notification(user_id=world.customer.id, type="new_offer", title_fr="other", body_fr="x"))
        await s.commit()

    r = await call(client, world.courier_user, "markNotificationsRead", {"all": True})
    assert r.status_code == 200 and r.json() == {"success": True, "marked": 130, "unread": 0}
    unread = await rows(
        select(Notification.id).where(
            Notification.user_id == world.courier_user.id, Notification.read_at.is_(None)
        )
    )
    assert unread == []
    others = await rows(select(Notification.read_at).where(Notification.user_id == world.customer.id))
    assert others == [None]  # someone else's rows never change
    # many rows: one re-count signal to his screens instead of 130 row events
    assert seen == [("Notification", "signal", {"kind": "all_read"})]
    again = await call(client, world.courier_user, "markNotificationsRead", {"all": True})
    assert again.json() == {"success": True, "marked": 0, "unread": 0}
    # the batch by ids still works as before
    assert (await call(client, world.courier_user, "markNotificationsRead", {"ids": []})).status_code == 400


# --- M11: the couriers who saw an order hear at once that it is closed (id only) ------------------------


async def test_m11_closed_signal_to_the_couriers_who_saw_it(client, world, factory, monkeypatch):
    from app.services import order_transitions

    seen = []
    original = order_transitions.emit

    def record(session, entity, type_, id_, audience=None, data=None):
        seen.append((entity, type_, str(id_), sorted(str(a) for a in audience or ()), data))
        original(session, entity, type_, id_, audience, data)

    monkeypatch.setattr(order_transitions, "emit", record)
    await device(world.courier_user)
    other_user = await factory.user(email="sami@example.test", full_name="Sami", profile=False)
    other = await world.make_courier(other_user, display_name="Sami B", phone_e164="+21655000999")
    placed = await call(client, world.customer, "placeOrder", {"order": order_form()})
    order_id = placed.json()["order"]["id"]
    # Karim and Sami were told; Sami offers, Karim's offer is accepted: Sami hears « closed », Karim not
    mine = await call(client, world.courier_user, "createOrderOffer", {"order_id": order_id, "fee": 4})
    theirs = await call(client, other_user, "createOrderOffer", {"order_id": order_id, "fee": 5})
    assert mine.status_code == theirs.status_code == 200
    seen.clear()
    r = await call(
        client,
        world.customer,
        "acceptOrderOffer",
        {"order_id": order_id, "offer_id": mine.json()["offer"]["id"]},
    )
    assert r.status_code == 200, r.text
    signals = [e for e in seen if e[1] == "signal"]
    assert signals == [
        (
            "Order",
            "signal",
            order_id,
            [str(other_user.id)],
            {"kind": "order_closed", "id": order_id, "status": "closed", "reason": "taken"},
        )
    ]
    assert other  # (his courier profile)

    # a cancelled open order: everyone told hears it, never the customer
    seen.clear()
    second = (await call(client, world.customer, "placeOrder", {"order": order_form()})).json()["order"]["id"]
    seen.clear()
    cancel = await call(
        client,
        world.customer,
        "cancelOrder",
        {"order_id": second, "cancelled_by": "customer", "reason": "changed_mind"},
    )
    assert cancel.status_code == 200, cancel.text
    [signal] = [e for e in seen if e[1] == "signal"]
    assert signal[3] == [str(other_user.id)]  # Karim, busy with the first order, was not told of it
    assert signal[4]["reason"] == "cancelled" and str(world.customer.id) not in signal[3]


# --- M12: one arrival per offer (announced if raised, else computed), one rank everywhere ---------------


async def test_m12_the_qa_capture_ranks_on_one_arrival(client, world, factory):
    """C1-1: courier 2 computed 27 announced 35, courier 1 computed 29 announced 29 → the customer saw 35
    above 29, no « + rapide », and courier 2 read « 1er » while ranked 2nd."""
    from sqlalchemy import update

    from app.db import SessionLocal
    from app.models import OrderOffer
    from tests.order_helpers import reload
    from tests.test_offer_arrival import rival

    order = await world.order(status="offers_received")
    one = await rival(world, factory, order, 1, "5", 29, 3, est=29)  # A: computed 29, announced 29
    me = {"order_id": str(order.id), "fee": 4, "eta": 35, "estimate": 27}  # computed 27, raised to 35
    body = (await call(client, world.courier_user, "getOfferRank", me)).json()
    assert body["my_arrival"] == 35
    assert (body["eta_rank"], body["eta_rank_shown"], body["eta_tied"], body["eta_tied_shown"]) == (
        2,
        2,
        0,
        0,
    )
    assert [(e["who"], e["eta_minutes"], e["fastest"]) for e in body["board"]] == [
        ("A", 29, True),
        ("me", 35, False),
    ]
    # an older offer typed under its computation (the former 2-min tolerance): it arrives at the computation
    async with SessionLocal() as s:
        await s.execute(
            update(OrderOffer)
            .where(OrderOffer.courier_id == one.id)
            .values(eta_minutes=27, estimated_eta_minutes=29)
        )
        await s.commit()
    # the customer's list reads the same arrival on each offer
    offers = (
        await client.get(
            "/api/entities/OrderOffer",
            params={"q": f'{{"order_id":"{order.id}"}}'},
            headers=auth(world.customer),
        )
    ).json()
    [a] = offers
    assert (a["eta_minutes"], a["estimated_eta_minutes"], a["arrival_minutes"], a["arrival_verified"]) == (
        27,
        29,
        29,
        True,
    )
    # accepted: the order's arrival is the offer's (never under the computation)
    r = await call(
        client, world.customer, "acceptOrderOffer", {"order_id": str(order.id), "offer_id": a["id"]}
    )
    assert r.status_code == 200, r.text
    assert (await reload(Order, order.id)).eta_minutes == 29


async def test_m12_no_reduction_even_one_minute(client, world):
    from tests.test_offer_arrival import NEAR

    order = await world.order()
    at = {"from_lat": NEAR[0], "from_lng": NEAR[1]}  # computed 15 min
    short = await call(
        client,
        world.courier_user,
        "createOrderOffer",
        {"order_id": str(order.id), "fee": 4, "eta_minutes": 14, **at},
    )
    assert short.status_code == 400 and short.json()["min_eta"] == 15
    raised = await call(
        client,
        world.courier_user,
        "createOrderOffer",
        {"order_id": str(order.id), "fee": 4, "eta_minutes": 20, **at},
    )
    assert raised.status_code == 200 and raised.json()["my_arrival"] == 20
    assert raised.json()["offer"]["arrival_minutes"] == 20
    from tests.order_helpers import notifications

    assert "chez vous ≈ 20 min" in (await notifications(world.customer, "new_offer"))[-1].body_fr


# --- M15: the menu shows 256 px WebP thumbnails, made once, cached in the bucket ------------------------


def png(size=992) -> bytes:
    import io

    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (size, size), (200, 120, 40)).save(out, "PNG")
    return out.getvalue()


async def test_m15_vitrine_thumbnails(client, world, monkeypatch):
    import io

    import httpx
    from PIL import Image
    from sqlalchemy import update

    from app.config import settings
    from app.db import SessionLocal
    from app.models import VitrineProduct
    from app.services import vitrine_thumbs
    from app.storage import s3
    from tests.order_helpers import reload
    from tests.test_vitrine_orders import public_vitrine

    shop_id, [cafe, croissant] = await public_vitrine(client, world.admin)
    photo = "https://ods-bucket.example/menu_images/menu_1.png"
    async with SessionLocal() as s:
        await s.execute(update(VitrineProduct).where(VitrineProduct.id == cafe).values(photo_key=photo))
        await s.execute(update(VitrineProduct).where(VitrineProduct.id == croissant).values(photo_key=photo))
        await s.commit()

    fetched, stored = [], {}
    source = {"status": 200}

    def answer(request):
        fetched.append(str(request.url))
        return httpx.Response(
            source["status"], content=png(), headers={"content-type": "binary/octet-stream"}
        )

    async def public(host, port):
        return True

    async def put(key, body, content_type, cache_control=None):
        stored[key] = (body, content_type, cache_control)

    monkeypatch.setattr(vitrine_thumbs, "_transport", httpx.MockTransport(answer))
    monkeypatch.setattr(vitrine_thumbs, "host_is_public", public)
    monkeypatch.setattr(s3, "put_object", put)

    menu = (await call(client, world.customer, "getVitrine", {"shop_id": shop_id})).json()["vitrine"]
    dish = next(p for p in menu["products"] if p["id"] == cafe)
    assert dish["photo_url"] == photo  # the original stays for the dish sheet
    base = settings.API_PUBLIC_URL.rstrip("/")
    assert dish["thumb_url"].startswith(f"{base}/api/public/vitrine/thumb/{cafe}?v=")

    # the original can't be read: the original photo instead, nothing remembered, tried again later
    source["status"] = 404
    miss = await client.get(f"/api/public/vitrine/thumb/{cafe}")
    assert miss.status_code == 302 and miss.headers["location"] == photo
    assert (await reload(VitrineProduct, uuid.UUID(cafe))).photo_thumb_key is None

    source["status"] = 200
    first = await client.get(dish["thumb_url"].removeprefix(base))
    assert first.status_code == 200 and first.headers["content-type"] == "image/webp"
    assert "max-age" in first.headers["cache-control"]
    image = Image.open(io.BytesIO(first.content))
    assert image.format == "WEBP" and max(image.size) == 256 and len(first.content) < 30_000
    [(key, (body, content_type, cache))] = stored.items()
    assert key.startswith("public/vitrine-thumbs/") and content_type == "image/webp" and body == first.content
    assert cache == vitrine_thumbs.CACHE_CONTROL
    # remembered on every dish with that photo: the menu now points at the bucket, made once
    for pid in (cafe, croissant):
        assert (await reload(VitrineProduct, uuid.UUID(pid))).photo_thumb_key == key
    menu = (await call(client, world.customer, "getVitrine", {"shop_id": shop_id})).json()["vitrine"]
    assert {p["thumb_url"] for p in menu["products"]} == {s3.public_url(key)}
    again = await client.get(f"/api/public/vitrine/thumb/{croissant}")
    assert again.status_code == 302 and again.headers["location"] == s3.public_url(key)
    assert len(fetched) == 2 and len(stored) == 1  # the 404 try, then one real fetch

    # a new photo: a new thumbnail
    async with SessionLocal() as s:
        await s.execute(
            update(VitrineProduct).where(VitrineProduct.id == cafe).values(photo_key=photo + "?v=2")
        )
        await s.commit()
    menu = (await call(client, world.customer, "getVitrine", {"shop_id": shop_id})).json()["vitrine"]
    assert next(p for p in menu["products"] if p["id"] == cafe)["thumb_url"].startswith(base)
    assert (await client.get(f"/api/public/vitrine/thumb/{uuid.uuid4()}")).status_code == 404


async def test_m15_thumbnail_source_is_read_with_care():
    from app.services import vitrine_thumbs

    assert await vitrine_thumbs.host_is_public("127.0.0.1", 443) is False
    assert await vitrine_thumbs.host_is_public("169.254.169.254", 443) is False
    assert await vitrine_thumbs.host_is_public("10.0.0.7", 443) is False
    with pytest.raises(vitrine_thumbs.ThumbFailed):
        await vitrine_thumbs.fetch_source("http://ods.example/a.png")  # https only
    with pytest.raises(vitrine_thumbs.ThumbFailed):
        await vitrine_thumbs.fetch_source("https://127.0.0.1/a.png")
    with pytest.raises(vitrine_thumbs.ThumbFailed):
        vitrine_thumbs.render(b"not an image")
    import io

    from PIL import Image

    assert max(Image.open(io.BytesIO(vitrine_thumbs.render(png(64)))).size) == 64  # never enlarged
