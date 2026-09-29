"""Phone verification by WhatsApp for foreign customer numbers (requestPhoneVerification,
confirmPhoneVerification), the UserProfile fields, placeOrder / reserveHotDeal / WhatsApp with a
verified foreign number, and the log line of a refused function. Meta is always mocked."""

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text, update

from app.db import SessionLocal
from app.models import Order, OutboundMessage, PhoneVerification, User
from app.services import whatsapp as wa
from tests.factories import auth
from tests.messaging_factories import ok_wa, wa_error
from tests.order_helpers import OrderWorld, reload
from tests.test_hot_deals import a_deal
from tests.test_order_flow_functions import order_form

FR = "+33612345678"
FR_TYPED = "+33 6 12 34 56 78"
UK = "+447400123456"


async def fn(client, user, name, body=None):
    return await client.post(f"/api/functions/{name}", json=body or {}, headers=auth(user))


async def profile(client, user):
    return (await client.get(f"/api/entities/UserProfile/{user.id}", headers=auth(user))).json()


def sent_code(http) -> str:
    body = json.loads(http.calls[-1].content)
    assert body["template"]["name"] == "ods_code_verification"
    return body["template"]["components"][0]["parameters"][0]["text"]


async def codes_of(user) -> list[PhoneVerification]:
    async with SessionLocal() as s:
        stmt = (
            select(PhoneVerification)
            .where(PhoneVerification.user_id == user.id)
            .order_by(PhoneVerification.created_at)
        )
        return list((await s.execute(stmt)).scalars())


@pytest.fixture
async def customer(factory):
    return await factory.user(email="expat@example.test", language="ar", phone_e164=FR)


@pytest.fixture
def meta(http, meta_on):
    http.responder = lambda request, n: ok_wa(f"wamid.{n}")
    return http


# --- requestPhoneVerification ------------------------------------------------------------------------


async def test_tunisian_numbers_need_no_code(client, customer, http):
    r = await fn(client, customer, "requestPhoneVerification", {"phone": "98 765 432"})
    assert r.status_code == 200 and r.json() == {"verified": True, "required": False}
    r = await fn(client, customer, "confirmPhoneVerification", {"phone": "+216 98765432", "code": "x"})
    assert r.status_code == 200 and r.json() == {"verified": True, "required": False}
    assert http.calls == [] and await codes_of(customer) == []


async def test_whatsapp_not_configured_is_503(client, customer, http):
    r = await fn(client, customer, "requestPhoneVerification", {"phone": FR_TYPED})
    assert r.status_code == 503
    assert r.json()["error"] == "whatsapp_unavailable" and "Tunisian" in r.json()["message"]
    assert http.calls == [] and await codes_of(customer) == []


@pytest.mark.parametrize(
    ("phone", "error"), [(None, "phone_required"), ("  ", "phone_required"), ("+33 1", "invalid_phone")]
)
async def test_request_validates_the_phone(client, customer, meta, phone, error):
    r = await fn(client, customer, "requestPhoneVerification", {"phone": phone})
    assert r.status_code == 400 and r.json()["error"] == error


async def test_landline_is_refused(client, customer, meta):
    r = await fn(client, customer, "requestPhoneVerification", {"phone": "+33 1 42 68 53 00"})
    assert r.status_code == 400 and r.json()["error"] == "invalid_phone" and meta.calls == []


async def test_happy_path_sends_by_whatsapp_and_verifies(client, factory, meta):
    user = await factory.user(email="new@example.test", language="ar")  # no phone yet
    r = await fn(client, user, "requestPhoneVerification", {"phone": FR_TYPED})
    assert r.status_code == 200, r.text
    assert r.json() == {"sent": True, "channel": "whatsapp", "expires_in": 600}
    [call] = meta.calls
    body = json.loads(call.content)
    assert body["to"] == "33612345678" and body["template"]["language"] == {"code": "ar"}
    code = sent_code(meta)
    assert len(code) == 6 and code.isdigit()
    [row] = await codes_of(user)
    assert row.phone_e164 == FR and code not in row.code_hash and row.used_at is None
    before = await profile(client, user)
    assert before["phone"] is None and before["phone_verified"] is False

    ok = await fn(client, user, "confirmPhoneVerification", {"phone": FR_TYPED, "code": f" {code} "})
    assert ok.status_code == 200 and ok.json() == {"verified": True, "phone": FR}
    doc = await profile(client, user)
    assert doc["phone"] == FR and doc["phone_verified"] is True
    assert doc["phone_verification_required"] is False
    again = await fn(client, user, "confirmPhoneVerification", {"phone": FR, "code": code})
    assert again.status_code == 200  # a double tap
    already = await fn(client, user, "requestPhoneVerification", {"phone": FR})
    assert already.json() == {"verified": True, "required": True} and len(meta.calls) == 1


async def test_wrong_code_counts_attempts_then_locks(client, customer, meta):
    await fn(client, customer, "requestPhoneVerification", {"phone": FR})
    code = sent_code(meta)
    wrong = "000000" if code != "000000" else "111111"
    r = await fn(client, customer, "confirmPhoneVerification", {"phone": FR, "code": wrong})
    assert r.status_code == 400 and r.json() == {"error": "invalid_code", "attempts_left": 4}
    [row] = await codes_of(customer)
    assert row.attempts == 1  # committed although refused
    for _ in range(3):
        await fn(client, customer, "confirmPhoneVerification", {"phone": FR, "code": "abc"})
    fifth = await fn(client, customer, "confirmPhoneVerification", {"phone": FR, "code": wrong})
    assert fifth.json() == {"error": "too_many_attempts"}
    right = await fn(client, customer, "confirmPhoneVerification", {"phone": FR, "code": code})
    assert right.status_code == 400 and right.json() == {"error": "too_many_attempts"}
    assert (await profile(client, customer))["phone_verified"] is False


async def test_expired_code(client, customer, meta):
    await fn(client, customer, "requestPhoneVerification", {"phone": FR})
    code = sent_code(meta)
    async with SessionLocal() as s:
        await s.execute(update(PhoneVerification).values(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
        await s.commit()
    r = await fn(client, customer, "confirmPhoneVerification", {"phone": FR, "code": code})
    assert r.status_code == 400 and r.json() == {"error": "code_expired"}


async def test_code_is_bound_to_its_number_and_a_new_code_voids_the_old(client, customer, meta):
    unknown = await fn(client, customer, "confirmPhoneVerification", {"phone": UK, "code": "123456"})
    assert unknown.json() == {"error": "invalid_code"}
    await fn(client, customer, "requestPhoneVerification", {"phone": FR})
    first = sent_code(meta)
    other = await fn(client, customer, "confirmPhoneVerification", {"phone": UK, "code": first})
    assert other.json() == {"error": "invalid_code"}
    await fn(client, customer, "requestPhoneVerification", {"phone": FR})
    second = sent_code(meta)
    if first != second:
        old = await fn(client, customer, "confirmPhoneVerification", {"phone": FR, "code": first})
        assert old.json()["error"] == "invalid_code"
    ok = await fn(client, customer, "confirmPhoneVerification", {"phone": FR, "code": second})
    assert ok.status_code == 200


async def test_rate_limit_per_user(client, customer, meta):
    for _ in range(3):
        assert (await fn(client, customer, "requestPhoneVerification", {"phone": FR})).status_code == 200
    r = await fn(client, customer, "requestPhoneVerification", {"phone": UK})
    assert r.status_code == 429
    assert r.json()["error"] == "too_many_codes" and r.json()["retry_after_seconds"] == 600
    assert "rate limit" not in r.json()["message"].lower() and len(meta.calls) == 3


async def test_rate_limit_per_number_and_day(client, factory, meta, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "MSG_LIMIT_PER_NUMBER_10MIN", 100)  # the messaging limit is looser
    users = [await factory.user(email=f"u{i}@example.test") for i in range(6)]
    for user in users[:5]:
        assert (await fn(client, user, "requestPhoneVerification", {"phone": UK})).status_code == 200
    r = await fn(client, users[5], "requestPhoneVerification", {"phone": UK})
    assert r.status_code == 429 and r.json()["retry_after_seconds"] == 86400


async def test_number_without_whatsapp_is_400_and_counted(client, customer, http, meta_on, sms_on):
    http.responder = lambda request, n: wa_error(131026)
    r = await fn(client, customer, "requestPhoneVerification", {"phone": FR})
    assert r.status_code == 400 and r.json()["error"] == "no_whatsapp"
    [row] = await codes_of(customer)
    assert row.used_at is not None  # kept for the limits, but dead
    assert all("winsms" not in str(c.url) for c in http.calls)  # no SMS to a foreign number


async def test_transient_failure_is_not_retried_later(client, customer, http, meta_on):
    http.responder = lambda request, n: wa_error(131000, 500)
    r = await fn(client, customer, "requestPhoneVerification", {"phone": FR})
    assert r.status_code == 502 and r.json()["error"] == "whatsapp_failed"
    async with SessionLocal() as s:
        [msg] = (await s.execute(select(OutboundMessage))).scalars()
    assert msg.status == "failed" and msg.purpose == "verification_code"


# --- UserProfile ---------------------------------------------------------------------------------------


async def verified(user, phone=FR) -> None:
    async with SessionLocal() as s:
        await s.execute(
            update(User)
            .where(User.id == user.id)
            .values(phone_e164=phone, phone_verified_at=datetime.now(UTC))
        )
        await s.commit()


@pytest.mark.parametrize(
    ("phone", "is_verified", "required"),
    [(None, False, False), ("+21698765432", True, False), (FR, False, True)],
)
async def test_profile_fields(client, factory, meta_on, phone, is_verified, required):
    user = await factory.user(phone_e164=phone)
    doc = await profile(client, user)
    assert doc["phone_verified"] is is_verified and doc["phone_verification_required"] is required
    assert doc["phone_verification_available"] is True


async def test_nothing_required_while_whatsapp_is_off(client, world, factory):
    """Owner's rule: without WhatsApp the code can't be sent, a foreign number is accepted."""
    expat = await factory.user(email="fr@example.test", phone_e164=FR)
    doc = await profile(client, expat)
    assert doc["phone_verified"] is False
    assert doc["phone_verification_required"] is False and doc["phone_verification_available"] is False
    placed = await fn(client, expat, "placeOrder", {"order": order_form()})
    assert placed.status_code == 200 and placed.json()["order"]["customer_phone"] == FR
    deal = await a_deal(world)
    reserved = await fn(
        client,
        world.customer,
        "reserveHotDeal",
        {"resale_order_id": str(deal.id), "delivery_address": "Rue X", "phone": "+44 7400 123456"},
    )
    assert reserved.status_code == 200, reserved.text
    nophone = await factory.user(email="np2@example.test")
    typed = await fn(client, nophone, "placeOrder", {"order": order_form(customer_phone=FR_TYPED)})
    assert typed.json() == {"error": "phone_required"}  # only the profile's number is taken


async def test_phone_change_resets_verification(client, customer, meta_on):
    await verified(customer)
    url = f"/api/entities/UserProfile/{customer.id}"
    same = await client.patch(url, json={"phone": FR_TYPED}, headers=auth(customer))
    assert same.status_code == 200 and same.json()["phone_verified"] is True
    # the flag can't be written by the client
    await client.patch(url, json={"phone_verified": True, "phone_verified_at": None}, headers=auth(customer))
    assert (await profile(client, customer))["phone_verified"] is True
    changed = await client.patch(url, json={"phone": UK}, headers=auth(customer))
    assert changed.json()["phone_verified"] is False and changed.json()["phone_verification_required"]
    back = await client.patch(url, json={"phone": FR}, headers=auth(customer))
    assert back.json()["phone_verified"] is False  # verified again only with a new code
    tunisian = await client.patch(url, json={"phone": "22 111 222"}, headers=auth(customer))
    assert tunisian.json()["phone_verified"] is True


async def test_trigger_clears_the_date_on_any_phone_write(customer):
    await verified(customer)
    async with SessionLocal() as s:
        await s.execute(text("UPDATE users SET phone_e164 = :p WHERE id = :u"), {"p": UK, "u": customer.id})
        await s.commit()
    assert (await reload(User, customer.id)).phone_verified_at is None


# --- placeOrder / reserveHotDeal -------------------------------------------------------------------------


@pytest.fixture
async def world(factory):
    return await OrderWorld(factory).setup()


async def test_place_order_refuses_an_unverified_foreign_number(client, world, factory, meta_on, caplog):
    expat = await factory.user(email="fr@example.test", phone_e164=FR)
    with caplog.at_level(logging.INFO, logger="odsd.functions"):
        r = await fn(client, expat, "placeOrder", {"order": order_form(customer_phone="+33 7 00 00 00 00")})
    assert r.status_code == 400 and r.json() == {"error": "phone_unverified"}
    [line] = [rec.getMessage() for rec in caplog.records if rec.name == "odsd.functions"]
    assert line == "function placeOrder refused 400 phone_unverified"
    async with SessionLocal() as s:
        assert (await s.execute(select(func.count()).select_from(Order))).scalar_one() == 0


async def test_place_order_typed_foreign_number_without_profile_phone(client, factory, meta_on):
    nophone = await factory.user(email="np@example.test")
    r = await fn(client, nophone, "placeOrder", {"order": order_form(customer_phone=FR_TYPED)})
    assert r.json() == {"error": "phone_unverified"}
    r = await fn(client, nophone, "placeOrder", {"order": order_form(customer_phone="12")})
    assert r.json() == {"error": "phone_required"}


async def test_place_order_accepts_a_verified_foreign_number(client, world, factory, meta_on):
    expat = await factory.user(email="fr@example.test", phone_e164=FR)
    await verified(expat)
    r = await fn(client, expat, "placeOrder", {"order": order_form()})
    assert r.status_code == 200, r.text
    assert r.json()["order"]["customer_phone"] == FR
    order = await reload(Order, uuid.UUID(r.json()["order"]["id"]))
    assert order.contact_phone_e164 == FR
    courier_view = await client.get(f"/api/entities/Order/{order.id}", headers=auth(world.courier_user))
    assert courier_view.status_code == 200


async def test_place_order_unverified_foreign_profile_may_give_a_tunisian_number(client, factory, meta_on):
    expat = await factory.user(email="fr@example.test", phone_e164=FR)
    r = await fn(client, expat, "placeOrder", {"order": order_form(customer_phone="98 765 432")})
    assert r.status_code == 200 and r.json()["order"]["customer_phone"] == "+21698765432"


async def test_reserve_hot_deal_with_foreign_numbers(client, world, factory, meta_on):
    deal = await a_deal(world)
    expat = await factory.user(email="fr@example.test", phone_e164=FR)
    body = {"resale_order_id": str(deal.id), "delivery_address": "Rue X"}
    refused = await fn(client, expat, "reserveHotDeal", body)
    assert refused.status_code == 400 and refused.json() == {"error": "phone_unverified"}
    typed = await fn(client, world.customer, "reserveHotDeal", {**body, "phone": "+44 7400 123456"})
    assert typed.json() == {"error": "phone_unverified"}
    await verified(expat)
    ok = await fn(client, expat, "reserveHotDeal", body)
    assert ok.status_code == 200, ok.text
    order = await reload(Order, uuid.UUID(ok.json()["order_id"]))
    assert order.contact_phone_e164 == FR


# --- WhatsApp to a verified foreign number ------------------------------------------------------------


async def test_whatsapp_reaches_a_verified_foreign_number_only(session, factory, meta, sms_on):
    expat = await factory.user(phone_e164=FR, whatsapp_opt_in_at=datetime.now(UTC))
    status, body = await wa.send_template(
        session,
        template_key="courier_on_the_way",
        params=["Sami", "Monoprix"],
        idempotency_key="k1",
        user_id=expat.id,
    )
    assert body["reason"] == "invalid_number" and meta.calls == []
    await verified(expat)
    session.expunge_all()
    status, body = await wa.send_template(
        session,
        template_key="customer_no_response",
        params=["Sami", "Monoprix", "+21655123456"],
        idempotency_key="k2",
        to=FR,
        user_id=expat.id,
        critical=True,
    )
    assert status == 200 and body["whatsapp"] == "sent"
    assert json.loads(meta.calls[0].content)["to"] == "33612345678"
    other = await wa.send_template(
        session,
        template_key="customer_no_response",
        params=["Sami", "Monoprix", "+21655123456"],
        idempotency_key="k3",
        to=UK,
        user_id=expat.id,
        critical=True,
    )
    assert other[1]["reason"] == "invalid_number"  # not his verified number


async def test_no_sms_fallback_to_a_foreign_number(session, factory, http, meta_on, sms_on):
    expat = await factory.user(phone_e164=FR)
    await verified(expat)
    http.responder = lambda request, n: wa_error(131026)
    _status, body = await wa.send_template(
        session,
        template_key="customer_no_response",
        params=["Sami", "Monoprix", "+21655123456"],
        idempotency_key="k4",
        user_id=expat.id,
        critical=True,
    )
    assert body["whatsapp"] == "failed" and body["fallback"] == "skipped"
    assert len(http.calls) == 1  # Meta only, no WinSMS
