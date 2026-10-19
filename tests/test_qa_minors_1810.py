"""QA campaign 18-19/10/2026, the minor and cosmetic findings fixed server side (m1 … m54, c1 … c6 of
the rapport technique) and the 4 « petits défauts » seen in prod (P1 … P4). One test per fix."""

from typing import Any

import pytest

from app.services import order_texts
from tests.factories import auth
from tests.order_helpers import OrderWorld, notifications


@pytest.fixture
async def world(factory):
    return await OrderWorld(factory).setup()


async def call(client, user, name: str, payload: dict[str, Any] | None = None):
    return await client.post(f"/api/functions/{name}", json=payload or {}, headers=auth(user))


async def order_doc(client, user, order) -> dict[str, Any]:
    r = await client.get(f"/api/entities/Order/{order.id}", headers=auth(user))
    assert r.status_code == 200, r.text
    return r.json()


# --- m7: Arabic minutes agree with the number, « حوالي » instead of « ~ » / « ≈ … دق » -----------------


def test_m7_arabic_minutes_in_the_order_notices():
    road = order_texts.on_the_way(5, "Monoprix", None)
    assert "حوالي 5 دقائق" in road["body_ar"] and "~" not in road["body_ar"]
    offer = order_texts.new_offer_for_customer("Karim Trabelsi", 4, 15)
    assert offer["body_ar"].endswith("الوصول إليك حوالي 15 دقيقة") and "دق " not in offer["body_ar"] + " "
    assert order_texts.new_offer_for_customer("Karim", 4, 2)["body_ar"].endswith("حوالي دقيقتين")


# --- m14: « Offre acceptée » of a Vitrine order: no « QA TEST [Vitrine] … — 3.200 DT » -------------------


def test_m14_offer_accepted_notice_is_clean():
    items = (
        "QA TEST [Vitrine] Coffee and Tea\n1× Espresso — 3.200 DT\n2× Croissant — 5.000 DT · Sans sucre\n"
        "Total articles : 8.200 DT"
    )
    text = order_texts.offer_accepted_for_courier("QA TEST Coffee and Tea", items)
    for body in (text["body_fr"], text["body_ar"]):
        assert "QA TEST" not in body and "[Vitrine]" not in body and "DT" not in body and "Total" not in body
    assert text["body_fr"] == "Coffee and Tea : 2 articles. Allez au magasin."
    assert text["body_ar"] == "Coffee and Tea: منتجان. توجّه إلى المتجر."
    one = order_texts.offer_accepted_for_courier("Monoprix", "2x Pain")
    assert one["body_fr"] == "Monoprix : 2x Pain. Allez au magasin."


# --- m43: « Rien n'est disponible » is never the FR / AR pair glued together ------------------------------


async def test_m43_nothing_available_without_the_glued_text(client, world):
    from tests.test_stock_checks import at_shop, report

    order = await at_shop(world)
    r = await report(client, world, order, missing_text="", nothing_available=True)
    assert r.status_code == 200, r.text
    assert r.json()["stock_check"]["missing_text"] == ""
    doc = await order_doc(client, world.customer, order)
    assert doc["stock_check"]["missing_text"] == "" and doc["stock_check"]["nothing_available"] is True
    [alert] = await notifications(world.customer, "stock_check")
    assert "/" not in alert.body_fr and "Rien n'est" not in alert.body_ar and "لا يتوفر" not in alert.body_fr
    assert alert.body_fr.startswith("Le livreur ne trouve pas votre commande. Répondez")
    # the courier's own words, when he wrote some, are kept
    assert (
        "(rayon vide)" in order_texts.stock_check_for_customer("rayon vide", None, None, True, 3)["body_fr"]
    )
