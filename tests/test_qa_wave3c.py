"""QA campaign 06/10, wave 3 (team c): cancellations after the purchase, incidents, Offres Chaudes.

B26 the courier who already paid the goods (bought, on the way, « client ne répond pas », or any
course bought from an Offre Chaude) can't just cancel: he resells (createHotDeal courier_choice) or
returns the goods to the shop, never sent to another courier · B30 the « rendu au magasin » notice
doesn't blame the courier · N3 the re-alert keeps the incident counted and says so · N13 one
« Dernière chance », then « Dernier appel sans réponse » · N11 the customer who did not answer
doesn't see his own order offered back."""

import uuid
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import select, text

from app.db import SessionLocal
from app.models import Courier, HotDeal, NoResponseCase, Order
from tests.factories import auth
from tests.order_helpers import SOUSSE_HOME, SOUSSE_SHOP, OrderWorld, notifications, reload, rows

FN = "/api/functions/{}"


@pytest.fixture
async def world(factory):
    w = await OrderWorld(factory).setup()
    w.buyer = await factory.user(email="buyer@example.test", full_name="Salma", phone_e164="+21698111222")
    return w


async def fn(client, user, name: str, body: dict[str, Any]) -> httpx.Response:
    return await client.post(FN.format(name), json=body, headers=auth(user))


async def cancel(client, world, order: Order, reason: str) -> httpx.Response:
    return await fn(
        client,
        world.courier_user,
        "cancelOrder",
        {
            "order_id": str(order.id),
            "reason": reason,
            "cancelled_by": "courier",
            "courier_id": str(world.courier.id),
        },
    )


async def policy(client, world, order: Order) -> dict[str, Any]:
    r = await fn(
        client, world.courier_user, "getCancellationPolicy", {"order_id": str(order.id), "actor": "courier"}
    )
    assert r.status_code == 200, r.text
    return r.json()["policy"]


async def hot_deal_course(client, world) -> tuple[Order, HotDeal]:
    """A course bought from an Offre Chaude: the deal of a resold order, reserved by the buyer."""
    source = await world.order(status="cancelled", courier=world.courier, purchase="12.345", items="Lait x2")
    async with SessionLocal() as s:
        deal = HotDeal(
            original_order_id=source.id, courier_id=world.courier.id, items_text="Lait x2",
            shop_name="Monoprix", purchase_amount=Decimal("12.345"), discount_percentage=Decimal("0"),
            price=Decimal("12.345"), start_price=Decimal("12.345"), floor_price=Decimal("12.345"),
            drop_step=Decimal("0"), delivery_fee=Decimal("5"),
            pickup_location=f"SRID=4326;POINT({SOUSSE_SHOP[1]} {SOUSSE_SHOP[0]})",
            expires_at=source.created_at.replace(year=source.created_at.year + 1),
        )  # fmt: skip
        s.add(deal)
        await s.commit()
        await s.refresh(deal)
    r = await fn(
        client,
        world.buyer,
        "reserveHotDeal",
        {"resale_order_id": str(deal.id), "delivery_address": "Rue X", "delivery_lat": SOUSSE_HOME[0],
         "delivery_lng": SOUSSE_HOME[1]},
    )  # fmt: skip
    assert r.status_code == 200, r.text
    order = await reload(Order, uuid.UUID(r.json()["order_id"]))
    assert order.status == "accepted" and order.resale_deal_id == deal.id
    return order, deal


# ─────────────────────────── B26 ───────────────────────────


@pytest.mark.parametrize("status", ["purchased", "on_the_way", "client_no_response"])
async def test_after_the_purchase_no_simple_cancel_only_resell_or_return(client, world, status):
    order = await world.order(status=status, courier=world.courier, fee="5", purchase="25")
    p = await policy(client, world, order)
    assert p["can_cancel"] is False and p["after_purchase"] is True and p["resell_or_return"] is True
    for reason in ("vehicle_issue", "emergency", "other reason"):
        r = await cancel(client, world, order, reason)
        assert r.status_code == 409 and r.json()["error"] == "after_purchase_resell_or_return"
    assert (await reload(Order, order.id)).status == status


async def test_returned_to_shop_closes_the_order_and_tells_the_customer(client, world):
    order = await world.order(status="on_the_way", courier=world.courier, fee="5", purchase="25")
    r = await cancel(client, world, order, "returned_to_shop")
    assert r.status_code == 200, r.text
    closed = await reload(Order, order.id)
    assert closed.status == "cancelled" and closed.courier_id == world.courier.id  # never back to the pool
    assert await notifications(type_="new_order") == []
    assert (await reload(Courier, world.courier.id)).late_cancellations == 1
    [note] = await notifications(world.customer, "order_cancelled")
    assert note.title_fr == "⚠️ Le livreur ne peut pas terminer la livraison"
    assert "rien à payer" in note.body_fr and note.data["fault_free"] is True


async def test_hot_deal_course_policy_and_no_simple_cancel(client, world):
    order, _deal = await hot_deal_course(client, world)
    p = await policy(client, world, order)
    assert p["after_purchase"] is True and p["resell_or_return"] is True and p["can_cancel"] is False
    r = await cancel(client, world, order, "vehicle_issue")
    assert r.status_code == 409 and r.json()["error"] == "after_purchase_resell_or_return"
    # still the same once on the way
    async with SessionLocal() as s:
        await s.execute(text("UPDATE orders SET status = 'on_the_way' WHERE id = :o"), {"o": order.id})
        await s.commit()
    assert (await cancel(client, world, order, "emergency")).status_code == 409


async def test_hot_deal_course_resold_again_by_the_courier(client, world):
    order, deal = await hot_deal_course(client, world)
    r = await fn(
        client, world.courier_user, "createHotDeal", {"order_id": str(order.id), "courier_choice": True}
    )
    assert r.status_code == 200, r.text
    closed = await reload(Order, order.id)
    assert closed.status == "cancelled" and closed.cancel_reason == "courier_resale"
    assert await rows(select(NoResponseCase)) == []  # the buyer is not blamed
    new_deal = await reload(HotDeal, uuid.UUID(r.json()["deal_id"]))
    assert new_deal.status == "available" and new_deal.original_order_id == order.id
    assert new_deal.purchase_amount == Decimal("12.345")
    assert (await reload(HotDeal, deal.id)).status == "expired"  # the deal it came from is over
    [note] = await notifications(world.buyer, "order_cancelled")
    assert (
        note.title_fr == "⚠️ Le livreur ne peut pas terminer la livraison" and note.data["fault_free"] is True
    )


async def test_hot_deal_course_returned_to_shop(client, world):
    order, deal = await hot_deal_course(client, world)
    r = await cancel(client, world, order, "returned_to_shop")
    assert r.status_code == 200, r.text
    assert (await reload(Order, order.id)).status == "cancelled"
    assert (await reload(HotDeal, deal.id)).status == "expired"  # never relisted
    [note] = await notifications(world.buyer, "order_cancelled")
    assert "rien à payer" in note.body_fr


# ─────────────────────────── B30 ───────────────────────────


async def test_returned_after_no_response_does_not_blame_the_courier(client, world):
    order = await world.order(status="client_no_response", courier=world.courier, fee="5", purchase="25")
    async with SessionLocal() as s:
        from datetime import UTC, datetime, timedelta

        past = datetime.now(UTC) - timedelta(minutes=10)
        s.add(
            NoResponseCase(
                order_id=order.id, courier_id=world.courier.id, status="expired", started_at=past,
                deadline_at=past + timedelta(minutes=3), final_at=past + timedelta(minutes=3),
                incident_counted=True, messaging_status="whatsapp_disabled",
            )
        )  # fmt: skip
        await s.commit()
    r = await cancel(client, world, order, "goods_returned_to_shop")
    assert r.status_code == 200, r.text
    [note] = await notifications(world.customer, "order_cancelled")
    assert note.title_fr == "❌ Commande annulée" and note.title_ar == "❌ تم إلغاء طلبك"
    assert "Le livreur a annulé" not in note.title_fr


# ─────────────────────────── N3 / N13 ───────────────────────────


async def test_last_alert_keeps_the_incident_and_is_announced_once(client, world):
    from tests.test_no_response import call, doc, incidents, on_the_way, rewind

    order = await on_the_way(world)
    await call(client, world.courier_user, "report_no_response", order)
    d = await doc(client, world.customer, order)
    assert d["no_response_attempts"] == 1 and d["no_response_incident_counted"] is False
    await rewind(order, 200)
    await call(client, world.courier_user, "status", order)
    assert await incidents(world.customer) == 1
    r = await call(client, world.courier_user, "realert_no_response", order)
    assert r.status_code == 200, r.text
    assert await incidents(world.customer) == 1  # still recorded during the last alert
    d = await doc(client, world.customer, order)
    assert d["no_response_attempts"] == 2 and d["no_response_incident_counted"] is True
    alerts = [
        n for n in await notifications(world.customer, "emergency_contact") if n.data["stage"] == "alert"
    ]
    assert "déjà enregistré" in alerts[-1].body_fr and "retiré si vous répondez" in alerts[-1].body_fr
    await rewind(order, 200)
    await call(client, world.courier_user, "status", order)
    finals = [
        n for n in await notifications(world.customer, "emergency_contact") if n.data["stage"] == "final"
    ]
    assert [n.title_fr for n in finals] == [
        "⚠️ Dernière chance : le livreur attend toujours",
        "❌ Dernier appel sans réponse",
    ]
    assert all("l'incident est retiré" in n.body_fr for n in finals)
    courier_finals = await notifications(world.courier_user, "customer_no_response_final")
    assert [n.title_fr for n in courier_finals] == [
        "❌ Le client ne répond toujours pas",
        "❌ Dernière alerte sans réponse",
    ]
    assert await incidents(world.customer) == 1  # one incident for the order


async def test_answering_the_last_alert_withdraws_the_incident(client, world):
    from tests.test_no_response import call, incidents, on_the_way, rewind

    order = await on_the_way(world)
    await call(client, world.courier_user, "report_no_response", order)
    await rewind(order, 200)
    await call(client, world.courier_user, "realert_no_response", order)
    assert await incidents(world.customer) == 1
    assert (await call(client, world.customer, "customer_confirms", order)).status_code == 200
    assert await incidents(world.customer) == 0  # what the texts promise: « retiré si vous répondez »


# ─────────────────────────── N11 ───────────────────────────


async def test_own_order_never_offered_back(client, world):
    source = await world.order(world.buyer, status="cancelled", courier=world.courier, purchase="20")
    async with SessionLocal() as s:
        deal = HotDeal(
            original_order_id=source.id, courier_id=world.courier.id, items_text="Pain x3",
            purchase_amount=Decimal("20"), discount_percentage=Decimal("0"), price=Decimal("20"),
            delivery_fee=Decimal("4"), pickup_location=f"SRID=4326;POINT({SOUSSE_SHOP[1]} {SOUSSE_SHOP[0]})",
            expires_at=source.created_at.replace(year=source.created_at.year + 1),
        )  # fmt: skip
        s.add(deal)
        await s.commit()
    mine = (await fn(client, world.buyer, "listHotDeals", {"radius_km": 50})).json()["deals"]
    assert mine == []
    other = (await fn(client, world.customer, "listHotDeals", {"radius_km": 50})).json()["deals"]
    assert [d["own_order"] for d in other] == [False]
    courier_view = (await fn(client, world.courier_user, "listHotDeals", {"radius_km": 50})).json()["deals"]
    assert courier_view == []
    detail = (await fn(client, world.buyer, "listHotDeals", {"id": str(deal.id)})).json()["deals"]
    assert len(detail) == 1 and detail[0]["own_order"] is True and detail[0]["own_deal"] is False
