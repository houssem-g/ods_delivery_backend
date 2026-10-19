"""QA prod 19/10 (remark 3): a courier who edits his offer WITHOUT a position (updateOrderOffer with no
from_lat / from_lng, no fresh server fix) kept losing the offer's computed arrival — the Arrival computed
without its courier → shop leg has no total and wiped estimated_eta_minutes / to_shop_km. The offer then
read « non vérifié » and fell to the bottom of the customer's list while his sheet said « 1er sur 3 ».
The previous computation is kept; the floor still uses it; both sides read the same rank."""

import pytest

from app.models import OrderOffer
from tests.factories import auth
from tests.order_helpers import SOUSSE_SHOP, OrderWorld, reload
from tests.test_offer_arrival import NEAR, call, place, rival


@pytest.fixture
async def world(factory):
    return await OrderWorld(factory).setup()


def customer_order(offers: list[dict]) -> list[str]:
    """The customer's list: verified offers by arrival (then price), the « non vérifié » ones last."""
    ranked = sorted(
        offers,
        key=lambda o: (not o["arrival_verified"], o["arrival_minutes"] or 10**6, float(o["proposed_fee"])),
    )
    return [o["id"] for o in ranked]


async def customer_list(client, world, order) -> list[dict]:
    r = await client.get(
        "/api/entities/OrderOffer",
        params={"q": f'{{"order_id":"{order.id}"}}'},
        headers=auth(world.customer),
    )
    assert r.status_code == 200, r.text
    return r.json()


async def test_price_edit_without_position_keeps_the_computed_arrival_and_the_rank(client, world, factory):
    await place(world, world.courier, SOUSSE_SHOP, seen_minutes_ago=600)  # no fresh server fix
    order = await world.order(status="offers_received")
    await rival(world, factory, order, 0, "4", 18, 5)  # A: verified, 18 min
    await rival(world, factory, order, 1, "4", 27, 4)  # B: verified, 27 min

    sent = await call(
        client,
        world.courier_user,
        "createOrderOffer",
        {"order_id": str(order.id), "fee": 3, "eta_minutes": 15, "from_lat": NEAR[0], "from_lng": NEAR[1]},
    )
    assert sent.status_code == 200, sent.text
    offer_id = sent.json()["offer"]["id"]
    before = await reload(OrderOffer, offer_id)
    assert (before.estimated_eta_minutes, float(before.to_shop_km)) == (15, 1.17)

    # the price alone, no position at all: the computation stays
    edited = await call(client, world.courier_user, "updateOrderOffer", {"offer_id": offer_id, "fee": 3.5})
    assert edited.status_code == 200, edited.text
    body = edited.json()
    after = await reload(OrderOffer, offer_id)
    assert (after.estimated_eta_minutes, float(after.to_shop_km), after.eta_minutes) == (15, 1.17, 15)
    assert body["my_verified"] is True and body["my_arrival"] == 15
    assert (body["eta_rank_shown"], body["total"]) == (1, 3)  # « 1er sur 3 à arriver »

    # the customer's list: the same offer, verified, first — the same rank as on the courier's sheet
    offers = await customer_list(client, world, order)
    mine = next(o for o in offers if o["id"] == offer_id)
    assert (mine["arrival_verified"], mine["arrival_minutes"], mine["estimated_eta_minutes"]) == (
        True,
        15,
        15,
    )
    assert customer_order(offers).index(offer_id) + 1 == body["eta_rank_shown"] == 1

    # his sheet reopened without a position reads the same rank
    sheet = (
        await call(client, world.courier_user, "getOfferRank", {"order_id": str(order.id), "fee": 3.5})
    ).json()
    assert (sheet["my_verified"], sheet["my_arrival"], sheet["eta_rank_shown"]) == (True, 15, 1)
    assert [e["who"] for e in sheet["board"]] == ["me", "A", "B"]

    # the owner's floor (19/10) still uses the kept computation: no reduction without a position either
    short = await call(
        client, world.courier_user, "updateOrderOffer", {"offer_id": offer_id, "fee": 3.5, "eta_minutes": 14}
    )
    assert short.status_code == 400
    assert short.json() == {"error": "eta_below_computed", "min_eta": 15, "computed": 15}
    # raising it is his announced time, still verified and ranked on it on both sides
    raised = await call(
        client, world.courier_user, "updateOrderOffer", {"offer_id": offer_id, "fee": 3.5, "eta_minutes": 30}
    )
    assert raised.status_code == 200, raised.text
    assert (raised.json()["my_verified"], raised.json()["my_arrival"], raised.json()["eta_rank_shown"]) == (
        True,
        30,
        3,
    )
    offers = await customer_list(client, world, order)
    assert (await reload(OrderOffer, offer_id)).estimated_eta_minutes == 15
    assert customer_order(offers).index(offer_id) + 1 == 3
