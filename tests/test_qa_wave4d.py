"""QA campaign 06/10, wave 4 (team d).

R30 the courier's name is written the same way in every customer notification: the new offer said
« QA 1. » (the initial of the last word, a digit) and the edited offer « QA Livreur 1 » (the raw
name). One function, order_texts.short_name, for both (and the « client ne répond pas » alert)."""

import pytest

from app.services.order_texts import new_offer_for_customer, short_name
from tests.factories import auth
from tests.order_helpers import OrderWorld, notifications


@pytest.fixture
async def world(factory):
    return await OrderWorld(factory).setup()


@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        ("Karim Trabelsi", "Karim T."),
        ("QA Livreur 1", "QA L."),  # never « QA 1. »
        ("QA 1", "QA"),  # no word with a letter after the first name: the first name alone
        ("  Ali   ben  Salah ", "Ali S."),
        ("Ali", "Ali"),
        ("", ""),
        (None, ""),
    ],
)
def test_short_name(raw, shown):
    assert short_name(raw) == shown


def test_new_offer_text_uses_short_name():
    text = new_offer_for_customer("QA Livreur 1", 7, None)
    assert text["body_fr"] == "QA L. propose : Achats + 7.000 DT"  # R2 wording
    assert text["body_ar"].startswith("QA L. ")


async def call(client, user, name, payload):
    return await client.post(f"/api/functions/{name}", json=payload, headers=auth(user))


async def test_new_and_edited_offer_name_the_courier_the_same_way(client, world):
    world.courier.display_name = "QA Livreur 1"
    from app.db import SessionLocal

    async with SessionLocal() as session:
        session.add(world.courier)
        await session.merge(world.courier)
        await session.commit()
    order = await world.order()
    r = await call(client, world.courier_user, "createOrderOffer", {"order_id": str(order.id), "fee": 7})
    assert r.status_code == 200, r.text
    offer_id = r.json()["offer"]["id"]
    r = await call(client, world.courier_user, "updateOrderOffer", {"offer_id": offer_id, "fee": 6.5})
    assert r.status_code == 200, r.text

    notes = await notifications(world.customer, "new_offer")
    assert len(notes) == 2
    for note in notes:
        assert note.body_fr.startswith("QA L. "), note.body_fr
        assert note.body_ar.startswith("QA L. "), note.body_ar
        assert "QA Livreur 1" not in note.body_fr and "QA 1." not in note.body_fr
