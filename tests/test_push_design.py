"""push_design: what the lock screen shows (owner, 17/10/2026 — « Fiche claire »). Lengths, cuts,
French / Arabic, the discreet « Test » mark, and the rule that a test order still reaches the QA
accounts only."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace as NS
from typing import Any

import pytest
from firebase_admin import messaging

from app.services import order_texts, push, push_design
from app.services.push import PushMessage, build_multicast
from app.services.push_design import LINE_MAX, TITLE_MAX, OrderCard, area_of, card_of, render, truncate
from tests.factories import auth
from tests.order_helpers import OrderWorld, device, notifications

CART = {
    "lines": [
        {"name": "Burger maison", "qty": 2, "mods": [{"label_fr": "Sans oignon", "label_ar": "بدون بصل"}]},
        {
            "name": "Jus d'orange",
            "qty": 1,
            "mods": [{"label_fr": "Peu de sucre", "label_ar": "قليل من السكر"}],
        },
        {"name": "Tiramisu", "qty": 1},
    ]
}
VITRINE_TEXT = (
    "QA TEST [Vitrine] Coffee and Tea\n2× Burger maison — 24.000 DT · Sans oignon\n"
    "1× Jus d'orange — 6.500 DT\n1× Tiramisu — 15.000 DT\nTotal articles : 45.500 DT"
)
STOP = NS(
    name="Coffee and Tea", address="Avenue Yasser Arafat, Khezama Est", city="Sousse", governorate="Sousse"
)


def an_order(**fields: Any) -> NS:
    base = {
        "cart": CART,
        "items_text": VITRINE_TEXT,
        "estimated_price": Decimal("45.5"),
        "budget_max": None,
        "delivery_address": "Rue Ibn Khaldoun, Sahloul 4",
        "delivery_city": "Sousse",
        "delivery_governorate": "Sousse",
        "source": "app",
    }
    return NS(**{**base, **fields})


def new_order(card: OrderCard, distance: float | None = 3.24, **metadata: Any) -> PushMessage:
    text = order_texts.new_order("QA TEST [Vitrine] Coffee and Tea 1× Bu", "Coffee and Tea", distance)
    meta = {
        "recipient_role": "courier",
        **({"distance_km": distance} if distance is not None else {}),
        **metadata,
    }
    return PushMessage(type="new_order", order_id="o1", metadata=meta, card=card, **text)


def encodes(multicast: messaging.MulticastMessage) -> None:
    one = messaging.Message(
        token="t",
        data=multicast.data,
        notification=multicast.notification,
        android=multicast.android,
        apns=multicast.apns,
        webpush=multicast.webpush,
    )
    messaging._MessagingService.encode_message(one)  # raises on anything FCM would refuse


# ─────────────────────────────── helpers ───────────────────────────────


def test_truncate_cuts_on_a_word():
    assert truncate("Coffee and Tea", 40) == "Coffee and Tea"
    cut = truncate("Le livreur ne peut pas terminer la livraison aujourd'hui", 30)
    assert len(cut) <= 30 and cut.endswith("…") and cut == "Le livreur ne peut pas…"
    assert truncate("Supercalifragilisticexpialidocious", 10) == "Supercali…"


def test_areas_known_in_french_and_arabic():
    assert area_of("Avenue Yasser Arafat, Khezama Est", "Sousse", "Sousse") == ("Khezama", "الخزامة")
    assert area_of("Rue 12, Sahloul 4", None, "Sousse") == ("Sahloul", "سهلول")
    assert area_of("Résidence Les Jasmins, Kalâa Kebira", None, None) == ("Kalâa Kebira", "القلعة الكبرى")
    assert area_of("Rue de la plage", "Sousse", "Sousse") == ("Sousse", "سوسة")
    assert area_of("QA TEST adresse client", None, None) == ("", "")
    assert area_of("Rue X", "Ezzahra", None) == ("Ezzahra", "Ezzahra")  # unknown: as written


def test_the_card_hides_the_test_words_but_remembers_the_test():
    card = card_of(an_order(), STOP, "https://cdn.example/burger.jpg")
    assert card.shop == "Coffee and Tea" and card.test is True
    assert card.shop_area == ("Khezama", "الخزامة") and card.dest_area == ("Sahloul", "سهلول")
    assert (
        card.lines_fr[0] == "2× Burger maison · Sans oignon"
        and card.lines_ar[0] == "\u20682× Burger maison\u2069 · بدون بصل"
    )
    assert card.goods == Decimal("45.5") and card.goods_exact is True
    assert card_of(an_order(), STOP, "http://insecure/x.jpg").image is None  # https only
    # a free-text order: its lines, without the « Total » row nor prices
    free = card_of(an_order(cart=None, items_text="2 baguettes\n1 L de lait"), STOP)
    assert (
        free.lines_fr == ("2 baguettes", "1 L de lait") and free.goods_exact is False and free.test is False
    )
    burger = {"name": "Burger", "qty": 1, "mods": [{"label_fr": "Sans oignon", "label_ar": "بدون بصل"}]}
    extras = {"lines": [{**burger, "extras": [{"key": "f", "name": "Fromage", "price": "1.500"}]}]}
    with_extras = card_of(an_order(cart=extras), STOP)
    assert with_extras.lines_fr == ("1× Burger · Sans oignon, + Fromage",)
    assert "+ Fromage" in with_extras.lines_ar[0] and "1.500" not in with_extras.lines_fr[0]
    many = card_of(an_order(cart={"lines": [{"name": f"Plat {i}", "qty": 1} for i in range(7)]}), STOP)
    assert len(many.lines_fr) == 4 and many.more == 3


# ─────────────────────────────── new course (courier) ───────────────────────────────


def test_new_course_says_where_how_far_and_how_much():
    card = card_of(an_order(items_text="[Vitrine] Coffee and Tea"), STOP)
    fr = render(new_order(card), "fr")
    assert fr.title == "Nouvelle course · 3,2 km · Khezama"
    first, *dishes = fr.body.split("\n")
    assert first == "Coffee and Tea → Sahloul · achat 45.500 DT"
    assert dishes == ["2× Burger maison · Sans oignon", "1× Jus d'orange · Peu de sucre", "1× Tiramisu"]
    assert fr.ios_subtitle == first and fr.ios_body.split("\n") == dishes
    assert fr.color == push_design.COURIER_COLOR and fr.renotify is True
    ar = render(new_order(card), "ar")
    assert ar.title == "طلب جديد · 3.2 كم · الخزامة"
    assert ar.body.split("\n")[0] == "من \u2068Coffee and Tea\u2069 إلى سهلول · شراء 45.500 د.ت"
    assert "بدون بصل" in ar.body and ar.lang == "ar"
    # a customer's estimate is « ≈ », never written as a sure price
    estimate = render(new_order(card_of(an_order(cart=None, items_text="2 pains"), STOP)), "fr")
    assert "achat ≈ 45.500 DT" in estimate.body


def test_no_emoji_and_no_qa_words_on_the_lock_screen():
    card = card_of(an_order(), STOP)
    for lang in ("fr", "ar"):
        view = render(new_order(card), lang)
        shown = view.title + view.body + (view.ios_subtitle or "") + view.ios_body
        assert "QA TEST" not in shown and "[Vitrine]" not in shown
        assert not push_design._EMOJI.search(shown)


def test_a_test_order_says_test_at_the_end_and_fits():
    card = card_of(an_order(), STOP)
    fr = render(new_order(card), "fr")
    assert fr.title.endswith(" · Test") and len(fr.title) <= TITLE_MAX
    assert fr.title == "Course · 3,2 km · Khezama · Test"  # « Nouvelle » goes first, the mark stays
    ar = render(new_order(card), "ar")
    assert ar.title.endswith(" · تجربة") and len(ar.title) <= TITLE_MAX
    long_area = OrderCard(shop="X", shop_area=("Sidi Abdelhamid Nord Extension", "x"), test=True)
    assert render(new_order(long_area), "fr").title.endswith("· Test")
    assert len(render(new_order(long_area), "fr").title) <= TITLE_MAX


@pytest.mark.parametrize("lang", ["fr", "ar"])
def test_lengths_hold_whatever_the_data(lang):
    card = OrderCard(
        shop="Restaurant Le Grand Palais des Saveurs Méditerranéennes de Sousse",
        shop_area=("Kalâa Kebira", "القلعة الكبرى"),
        dest_area=("Port El Kantaoui Marina Résidence", "القنطاوي"),
        goods=Decimal("1234.5"),
        lines_fr=tuple(f"3× Plat du jour très long numéro {i} · Sans harissa, Sans oignon" for i in range(4)),
        lines_ar=tuple(f"3× طبق اليوم {i} · بدون هريسة، بدون بصل" for i in range(4)),
        more=6,
        test=True,
    )
    view = render(new_order(card, distance=12.345), lang)
    assert len(view.title) <= TITLE_MAX
    assert all(len(row) <= LINE_MAX for row in view.body.split("\n"))
    assert len(view.body) <= push_design.BODY_MAX
    assert view.body.split("\n")[-1] in ("+ 6 autres articles", "+ 6 منتجات أخرى")


def test_unknown_distance_and_preferred_client():
    card = card_of(an_order(items_text="[Vitrine] Coffee and Tea"), STOP)
    assert render(new_order(card, distance=None), "fr").title == "Nouvelle course · Khezama"
    preferred = render(new_order(card, preferred_courier=True), "fr")
    assert preferred.title == "Votre client · 3,2 km · Khezama"  # « Course de votre client » is 41
    assert render(new_order(card, preferred_courier=True), "ar").title.startswith("طلب من حريفك")


def test_a_photo_only_when_it_hides_no_dish():
    with_dishes = card_of(an_order(), STOP, "https://cdn.example/b.jpg")
    assert render(new_order(with_dishes), "fr").image is None
    no_dishes = card_of(an_order(cart=None, items_text=""), STOP, "https://cdn.example/shop.jpg")
    assert render(new_order(no_dishes), "fr").image == "https://cdn.example/shop.jpg"


# ─────────────────────────────── customer side ───────────────────────────────


def offer(card: OrderCard, **metadata: Any) -> PushMessage:
    text = order_texts.new_offer_for_customer("Karim Trabelsi", Decimal("5"), 15)
    meta = {"courier_name": "Karim Trabelsi", "proposed_fee": 5.0, "eta_minutes": 15, **metadata}
    return PushMessage(type="new_offer", order_id="o1", metadata=meta, card=card, **text)


def test_offer_received_then_several_offers_in_one_notification():
    card = OrderCard(
        shop="Coffee and Tea", image="https://cdn.example/shop.jpg", offers=1, best_fee=Decimal("5")
    )
    one = render(offer(card), "fr")
    assert one.title == "Nouvelle offre · Karim T. · ≈ 15 min"
    assert one.body.split("\n")[-1] == "Chez vous ≈ 15 min. Touchez pour voir et accepter."
    assert (
        one.body.split("\n")[0] == "Livraison 5.000 DT + vos achats · Coffee and Tea"
    )  # never the fee alone
    assert one.image == "https://cdn.example/shop.jpg" and one.color == push_design.CUSTOMER_COLOR
    three = render(offer(OrderCard(shop="Coffee and Tea", offers=3, best_fee=Decimal("4"))), "fr")
    assert three.title == "3 offres reçues · dès 4.000 DT" and three.count == 3
    assert three.body.startswith("Karim T. : livraison 5.000 DT + vos achats")
    ar = render(offer(OrderCard(shop="Coffee and Tea", offers=2, best_fee=Decimal("4"))), "ar")
    assert ar.title == "عرضان · ابتداءً من 4.000 د.ت"
    edited = render(offer(card, kind="offer_updated"), "fr")
    assert edited.title.startswith("Offre modifiée")


def test_courier_at_the_door():
    deadline = (datetime.now(UTC) + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    meta = {"stage": "alert", "attempt": 1, "deadline_at": deadline, "recipient_role": "customer"}
    msg = PushMessage(
        type="emergency_contact", order_id="o1", metadata=meta, card=OrderCard(shop="Coffee and Tea"),
        title_fr="🚨 Urgent : le livreur est devant chez vous !", title_ar="🚨 عاجل: المندوب أمام منزلك!",
        body_fr="Votre livreur Karim T. a avancé l'argent… Un incident de non-réponse est déjà enregistré",
        body_ar="المندوب … سُجّلت حادثة عدم رد",
    )  # fmt: skip
    fr = render(msg, "fr")
    assert fr.title == "Votre livreur est devant chez vous" and fr.color == push_design.URGENT_COLOR
    assert fr.body.split("\n")[0] == "Répondez sous 5 min : appelez-le ou confirmez"
    assert "incident" in fr.body
    assert render(msg, "ar").title == "المندوب أمام منزلك"
    last = render(PushMessage(**{**msg.__dict__, "metadata": {**meta, "attempt": 2}}), "fr")
    assert last.title.startswith("Dernier appel") and len(last.title) <= TITLE_MAX


def test_every_other_notice_keeps_its_words_without_the_emoji():
    msg = PushMessage(
        type="purchased", order_id="o1", metadata={"recipient_role": "customer"}, card=None,
        title_fr="🛍️ Achat effectué · Coffee and Tea", title_ar="🛍️ تم الشراء · Coffee and Tea",
        body_fr="Montant des achats : 45.500 DT (selon le reçu). À payer : 50.500 DT", body_ar="…",
    )  # fmt: skip
    view = render(msg, "fr")
    assert view.title == "Achat effectué · Coffee and Tea"
    assert view.body == "Montant des achats : 45.500 DT (selon le reçu). À payer : 50.500 DT"
    assert view.ios_subtitle is None and view.ios_body == view.body
    long_body = "x " * 200
    assert (
        len(render(PushMessage(**{**msg.__dict__, "body_fr": long_body}), "fr").body) <= push_design.BODY_MAX
    )
    # a test order's generic notice: mark visible within the lock-screen width
    test = PushMessage(**{**msg.__dict__, "title_fr": "⚠️ Le livreur ne peut pas terminer la livraison",
                          "body_fr": "QA TEST pain : annulé"})  # fmt: skip
    shown = render(test, "fr")
    assert shown.title.endswith(" · Test") and len(shown.title) <= TITLE_MAX and "QA TEST" not in shown.body


# ─────────────────────────────── the FCM message ───────────────────────────────


def test_the_message_carries_the_design_on_every_platform(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "PUBLIC_APP_URL", "https://delivery.example.test")
    card = card_of(an_order(), STOP)
    m = build_multicast(["tok"], "fr", new_order(card))
    assert m.notification.title == "Course · 3,2 km · Khezama · Test"
    assert m.android.notification.color == push_design.COURIER_COLOR
    assert m.android.notification.icon == "ic_stat_ods"  # the logo; older shells fall back to theirs
    assert m.android.notification.tag == m.android.collapse_key == "order_o1"  # one per order on screen
    assert m.android.notification.channel_id == "new_orders"
    alert = m.apns.payload.aps.alert
    assert (
        alert.subtitle.startswith("Coffee and Tea → Sahloul") and m.apns.payload.aps.thread_id == "order_o1"
    )
    web = m.webpush.notification
    assert web.renotify is True and web.tag == "order_o1" and web.direction == "ltr" and web.badge
    assert m.data["lang"] == "fr" and m.data["title_fr"].startswith("🎯")  # the in-app row's text, as before
    encodes(m)
    ar = build_multicast(["tok"], "ar", offer(OrderCard(shop="Coffee", image="https://cdn.example/s.jpg")))
    assert ar.webpush.notification.direction == "rtl" and ar.notification.image == "https://cdn.example/s.jpg"
    encodes(ar)


# ─────────────────────────────── a real dispatch ───────────────────────────────


class Recorder:
    name = "log"

    def __init__(self) -> None:
        self.sent: list[tuple[str, messaging.MulticastMessage]] = []

    async def send(self, tokens, msg):
        for locale in ("fr", "ar"):
            self.sent.append((locale, build_multicast([t.token for t in tokens], locale, msg)))
        return [push.Outcome(t.id, "sent") for t in tokens]


@pytest.fixture
async def world(factory):
    return await OrderWorld(factory).setup()


async def test_a_test_order_still_reaches_the_qa_couriers_only(client, world, factory, monkeypatch):
    from app.config import settings

    qa_user = await factory.user(email="qa-push@example.test", full_name="QA Livreur", profile=False)
    await world.make_courier(qa_user, display_name="QA Livreur", phone_e164="+21655999877")
    monkeypatch.setattr(settings, "QA_ACCOUNTS", ["qa-push@example.test"])
    await device(qa_user)
    await device(world.courier_user)
    recorder = Recorder()
    monkeypatch.setattr(push, "get_provider", lambda: recorder)
    order = await world.order(items=VITRINE_TEXT, estimated_price=Decimal("45.5"))
    r = await client.post(
        "/api/functions/dispatchOrderToCouriers",
        json={"order_id": str(order.id)},
        headers=auth(world.customer),
    )
    assert r.json()["dispatched"] == 1 and r.json()["qa_only"] is True
    assert await notifications(world.courier_user) == []  # the real courier gets nothing
    assert len(await notifications(qa_user, "new_order")) == 1
    (_, fr), (_, ar) = recorder.sent
    assert fr.notification.title.startswith("Nouvelle course") or fr.notification.title.startswith("Course")
    assert fr.notification.title.endswith(" · Test") and len(fr.notification.title) <= TITLE_MAX
    assert "QA TEST" not in fr.notification.body and fr.notification.body.startswith("Monoprix")
    assert "2× Burger maison" in fr.notification.body
    assert ar.notification.title.endswith(" · تجربة")
