"""WhatsApp (Meta) + SMS (WinSMS): the Deno messaging_test.ts cases against the real database,
the webhook route (signature, challenge), the admin function and the messaging jobs.
Every HTTP call goes to an httpx.MockTransport (fixture `http`)."""

import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from sqlalchemy import select, update

from app.api.webhooks import hmac_sha256_hex, verify_challenge, verify_signature
from app.config import settings
from app.db import SessionLocal
from app.jobs import messaging as messaging_jobs
from app.jobs.registry import JOBS
from app.models import OutboundMessage, User
from app.services import whatsapp as wa
from tests.factories import auth
from tests.messaging_factories import make_order, ok_wa, sms_ok, wa_error

ALERT = {
    "template_key": "customer_no_response",
    "params": ["Sami", "Carrefour", "+216 98 123 456"],
    "to": "98 765 432",
    "idempotency_key": "noresp:o1:c1",
    "critical": True,
}


async def send(session, **overrides):
    return await wa.send_template(session, **{**ALERT, **overrides})


async def log_rows(session):
    # rows of one transaction share created_at: parents first, then by key
    stmt = select(OutboundMessage).order_by(
        OutboundMessage.created_at, OutboundMessage.parent_id.is_not(None), OutboundMessage.idempotency_key
    )
    return list((await session.execute(stmt)).scalars())


async def opted_in(factory, opt_in=True, **fields):
    return await factory.user(
        phone_e164=fields.pop("phone_e164", "+21698765432"),
        whatsapp_opt_in_at=datetime.now(UTC) if opt_in else None,
        **fields,
    )


# ─────────────────────────── pure helpers ───────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("98 765 432", "+21698765432"),
        ("+216 22-123-456", "+21622123456"),
        ("0021655123456", "+21655123456"),
        ("21641123456", "+21641123456"),
        ("(+216) 50.123.456", "+21650123456"),
        ("71 123 456", None),
        ("+216 31 123 456", None),
        ("80 100 200", None),
        ("+33 6 12 34 56 78", None),
        ("+2169876543", None),
        ("98765432a", None),
        ("", None),
        (None, None),
        ("+", None),
    ],
)
def test_tunisian_mobiles_only(raw, expected):
    assert wa.normalize_tunisian_mobile(raw) == expected


def test_template_payload_and_params():
    assert wa.sanitize_param("a\n\tb     c") == "a b c"
    assert wa.sanitize_param("") == "-" and wa.sanitize_param(None) == "-"
    payload = wa.build_template_payload("+21698765432", "tpl", "fr", ["1", "2"])
    assert payload["to"] == "21698765432" and payload["template"]["language"]["code"] == "fr"
    assert payload["template"]["components"][0]["parameters"][1]["text"] == "2"
    otp = wa.build_template_payload("+21698765432", "otp", "ar", ["4821"], copy_code=True)
    assert otp["template"]["components"][1]["sub_type"] == "url"
    assert otp["template"]["components"][1]["parameters"][0]["text"] == "4821"
    assert "components" not in wa.build_template_payload("+21698765432", "x", "fr", [])["template"]


def test_error_classes_and_status_order():
    assert wa.classify_whatsapp_error(400, 131026) == "no_whatsapp"
    assert wa.classify_whatsapp_error(429, 130429) == "transient"
    assert wa.classify_whatsapp_error(500, 131000) == "transient"
    assert wa.classify_whatsapp_error(400, 132001) == "permanent"
    assert wa.classify_whatsapp_error(0) == "transient" and wa.classify_whatsapp_error(503) == "transient"
    assert wa.classify_whatsapp_error(400) == "permanent"
    for current, incoming, expected in [
        ("sent", "delivered", "delivered"),
        ("read", "delivered", "read"),
        ("delivered", "sent", "delivered"),
        ("sent", "failed", "failed"),
        ("delivered", "failed", "delivered"),
        ("failed", "read", "read"),
        ("failed", "sent", "failed"),
        ("sent", "weird", "sent"),
    ]:
        assert wa.next_status(current, incoming) == expected


def test_winsms_reply_parsing():
    assert wa.parse_winsms_response(200, '{"code":"ok","ref":"9"}') == {"ok": True, "ref": "9"}
    assert wa.parse_winsms_response(200, '{"code":"102","message":"Invalid API key"}')["ok"] is False
    assert wa.parse_winsms_response(200, '{"data":{"id":7}}') == {"ok": True, "ref": "7"}
    assert wa.parse_winsms_response(200, "Solde insuffisant")["ok"] is False
    assert wa.parse_winsms_response(200, "OK")["ok"] is True
    assert wa.parse_winsms_response(500, "oops")["ok"] is False


def test_sms_texts():
    fr = wa.TEMPLATES["customer_no_response"].sms(["Sami", "Carrefour", "+216 1"], "fr")
    assert fr.startswith("ODS: votre livreur Sami est devant chez vous avec votre commande (Carrefour)")
    assert wa.TEMPLATES["customer_no_response"].sms(["a", "b", "c"], "ar").startswith("ODS: المندوب a")
    assert wa.TEMPLATES["verification_code"].sms(["1234"], "ar") == "ODS: رمز التحقق 1234"


# ─────────────────────────── send ───────────────────────────


async def test_off_without_secrets_nothing_leaves(session, http):
    status, body = await send(session)
    assert status == 200 and body["whatsapp"] == "disabled" and body["fallback"] == "disabled"
    assert http.calls == []
    assert [(r.channel, r.status) for r in await log_rows(session)] == [
        ("whatsapp", "disabled"),
        ("sms", "disabled"),
    ]
    await session.rollback()


async def test_kill_switch(session, http, meta_on, sms_on, monkeypatch):
    monkeypatch.setattr(settings, "MESSAGING_DISABLED", True)
    _status, body = await send(session)
    assert body["whatsapp"] == "disabled" and http.calls == []
    await session.rollback()


async def test_whatsapp_off_sms_on_critical_alert_goes_by_sms(session, http, sms_on):
    http.responder = lambda _r, _n: sms_ok("r1")
    _status, body = await send(session)
    assert body["fallback"] == "sent" and len(http.calls) == 1
    url = urlparse(str(http.calls[0].url))
    query = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert url.hostname == "www.winsmspro.com"
    assert (query["action"], query["to"], query["from"]) == ("send-sms", "21698765432", "ODS")
    assert "+216 98 123 456" in query["sms"]
    whatsapp_row, sms_row = await log_rows(session)
    assert sms_row.provider_message_id == "r1" and sms_row.parent_id == whatsapp_row.id
    assert whatsapp_row.fallback_status == "sent"
    await session.rollback()


async def test_sends_the_template_and_idempotency_blocks_a_second_send(session, http, meta_on):
    http.responder = lambda _r, _n: ok_wa()
    _status, first = await send(session)
    assert first["whatsapp"] == "sent" and first["provider_message_id"] == "wamid.1"
    request = http.calls[0]
    assert str(request.url) == "https://graph.facebook.com/v21.0/123/messages"
    assert request.headers["authorization"] == "Bearer t"
    sent = json.loads(request.content)
    assert sent["template"]["name"] == "ods_livreur_injoignable" and sent["to"] == "21698765432"
    [row] = await log_rows(session)
    assert row.status == "sent" and row.fallback_status == "pending" and row.fallback_deadline_at
    _status, again = await send(session)
    assert again["duplicate"] is True and again["status"] == "sent" and len(http.calls) == 1
    await session.rollback()


async def test_input_errors(session):
    assert (await send(session, template_key="nope"))[1] == {"error": "unknown template_key"}
    assert (await send(session, idempotency_key=""))[1] == {"error": "idempotency_key required"}
    assert (await send(session, params=["x"]))[1] == {"error": "template needs 3 params"}


async def test_invalid_number_and_opt_in(session, http, meta_on, factory):
    http.responder = lambda _r, _n: ok_wa()
    _s, bad = await send(session, to="71 000 000", idempotency_key="k1")
    assert bad["reason"] == "invalid_number"
    user = await opted_in(factory, opt_in=False, phone_e164="+21622111222")
    _s, offer = await send(
        session,
        template_key="new_offer",
        params=["Sami", "5.000"],
        to=None,
        user_id=user.id,
        idempotency_key="k2",
        critical=False,
    )
    assert offer["reason"] == "no_opt_in" and http.calls == []
    # the critical alert does not need the marketing opt-in
    _s, crit = await send(session, to=None, user_id=user.id, idempotency_key="k3")
    assert crit["whatsapp"] == "sent"
    # a refused key stays taken
    _s, dup = await send(session, to="71 000 000", idempotency_key="k1")
    assert dup["duplicate"] is True
    await session.rollback()


async def test_user_language_and_template_override(session, http, meta_on, factory, monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_TPL_NO_RESPONSE", "custom_tpl")
    http.responder = lambda _r, _n: ok_wa()
    user = await opted_in(factory, language="ar")
    await send(session, to=None, user_id=user.id)
    sent = json.loads(http.calls[0].content)
    assert sent["template"]["language"]["code"] == "ar" and sent["template"]["name"] == "custom_tpl"
    await session.rollback()


async def test_per_number_rate_limit(session, http, meta_on):
    http.responder = lambda _r, _n: ok_wa()
    for i in range(4):
        assert (await send(session, idempotency_key=f"k{i}"))[1]["whatsapp"] == "sent"
    _s, fifth = await send(session, idempotency_key="k5")
    assert fifth["reason"] == "rate_limited" and fifth["limit"] == "per_number" and len(http.calls) == 4
    await session.rollback()


async def test_global_rate_limit_counts_only_real_sends(session, http, meta_on, monkeypatch):
    monkeypatch.setattr(settings, "MSG_LIMIT_GLOBAL_MINUTE", 3)
    for i, status in enumerate(["disabled", "rate_limited", "sent", "sent"]):
        session.add(
            OutboundMessage(channel="whatsapp", purpose="new_offer", to_e164=f"+2169800000{i}", status=status)
        )
    await session.flush()
    http.responder = lambda _r, _n: ok_wa()
    assert (await send(session, idempotency_key="g1"))[1]["whatsapp"] == "sent"
    _s, limited = await send(session, idempotency_key="g2", to="22 111 333")
    assert limited["limit"] == "global"
    await session.execute(update(OutboundMessage).values(created_at=datetime.now(UTC) - timedelta(minutes=5)))
    assert (await send(session, idempotency_key="g3", to="22 111 333"))[1]["whatsapp"] == "sent"
    monkeypatch.setattr(settings, "MSG_LIMIT_GLOBAL_HOUR", 1)
    assert (await send(session, idempotency_key="g4", to="22 111 444"))[1]["limit"] == "global"
    monkeypatch.setattr(settings, "MSG_LIMIT_PER_NUMBER_DAY", 1)
    assert (await send(session, idempotency_key="g5", to="22 111 333"))[1]["limit"] == "per_number"
    await session.rollback()


async def test_transient_errors_are_retried_then_succeed(session, http, meta_on):
    http.responder = lambda _r, n: wa_error(131000, 500) if n < 3 else ok_wa("wamid.ok")
    _s, body = await send(session)
    assert body["whatsapp"] == "sent" and len(http.calls) == 3
    [row] = await log_rows(session)
    assert row.attempts == 3
    await session.rollback()


async def test_network_error_is_transient(session, http, meta_on, sms_on):
    def responder(request, _n):
        if "graph.facebook.com" in str(request.url):
            raise httpx.ConnectError("down")
        return httpx.Response(200, text="OK")

    http.responder = responder
    _s, body = await send(session)
    assert body["whatsapp"] == "failed" and body["error_code"] == "network" and body["fallback"] == "sent"
    await session.rollback()


async def test_no_whatsapp_account_immediate_sms(session, http, meta_on, sms_on):
    http.responder = lambda r, _n: wa_error(131026) if "graph.facebook.com" in str(r.url) else sms_ok("s1")
    _s, body = await send(session, critical=False)
    assert body["whatsapp"] == "failed" and body["fallback"] == "sent" and len(http.calls) == 2
    assert (await log_rows(session))[0].fallback_status == "sent"
    await session.rollback()


async def test_permanent_failure_without_fallback_and_failed_sms(session, http, meta_on, sms_on):
    http.responder = lambda r, _n: (
        wa_error(132001)
        if "graph.facebook.com" in str(r.url)
        else httpx.Response(200, json={"code": "102", "message": "Invalid API key"})
    )
    _s, quiet = await send(session, critical=False, idempotency_key="p1")
    assert quiet["whatsapp"] == "failed" and "fallback" not in quiet
    _s, loud = await send(session, idempotency_key="p2", to="22 000 111")
    assert loud["fallback"] == "failed"
    sms = next(r for r in await log_rows(session) if r.channel == "sms")
    assert sms.status == "failed" and sms.error_message == "Invalid API key"
    await session.rollback()


async def test_sms_retry_on_server_error(session, http, sms_on):
    http.responder = lambda _r, n: httpx.Response(502, text="bad gateway") if n == 1 else sms_ok()
    _s, body = await send(session)
    assert body["fallback"] == "sent" and len(http.calls) == 2
    await session.rollback()


async def test_non_critical_transient_failure_waits_for_check_pending(session, http, meta_on, factory):
    fail = {"on": True}
    http.responder = lambda _r, _n: wa_error(130429, 429) if fail["on"] else ok_wa("wamid.late")
    user = await opted_in(factory)
    _s, body = await wa.send_template(
        session, template_key="new_offer", params=["Sami", "5.000"], idempotency_key="k", user_id=user.id
    )
    assert body["whatsapp"] == "retry_pending"
    fail["on"] = False
    await wa.check_pending(session)
    assert (await log_rows(session))[0].status == "retry_pending"  # not yet due
    await session.execute(
        update(OutboundMessage).values(next_attempt_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    _s, out = await wa.check_pending(session)
    assert out["retried"] == 1
    [row] = await log_rows(session)
    await session.refresh(row)
    assert row.status == "sent" and row.attempts == 4
    await session.rollback()


async def test_check_pending_gives_up_and_keeps_transient_retries(session, http, meta_on):
    http.responder = lambda _r, _n: wa_error(4, 500)
    for key, attempts, purpose in (("a", 9, "new_offer"), ("b", 3, "new_offer"), ("c", 1, "gone")):
        session.add(
            OutboundMessage(
                channel="whatsapp",
                purpose=purpose,
                to_e164="+21698765432",
                idempotency_key=key,
                status="retry_pending",
                attempts=attempts,
                params=["x", "y"],
            )
        )
    await session.flush()
    _s, out = await wa.check_pending(session)
    assert out["retried"] == 1
    status = {r.idempotency_key: (r.status, r.attempts) for r in await log_rows(session)}
    assert status == {"a": ("failed", 9), "b": ("retry_pending", 6), "c": ("retry_pending", 1)}
    await session.rollback()


async def test_critical_message_not_delivered_in_time_gets_one_sms(session, http, meta_on, sms_on, parties):
    order = await make_order(parties.customer, parties.courier)
    http.responder = lambda r, _n: ok_wa("wamid.x") if "graph.facebook.com" in str(r.url) else sms_ok()
    await send(session, order_id=order.id)
    assert (await wa.check_pending(session, order.id))[1]["fallbacks"] == 0  # deadline not reached
    await session.execute(
        update(OutboundMessage).values(fallback_deadline_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    assert (await wa.check_pending(session, order.id))[1]["fallbacks"] == 1
    assert (await wa.check_pending(session, order.id))[1]["fallbacks"] == 0
    assert len([c for c in http.calls if "winsms" in str(c.url)]) == 1
    _s, summary = await wa.summary(session, "noresp:o1:c1")
    assert summary == {
        "found": True,
        "whatsapp": "sent",
        "whatsapp_error": None,
        "sms": "sent",
        "fallback_status": "sent",
    }
    assert (await wa.summary(session, "unknown"))[1] == {"found": False}
    await session.rollback()


async def test_delivered_in_time_no_sms(session, http, meta_on, sms_on):
    http.responder = lambda _r, _n: ok_wa("wamid.d")
    await send(session)
    result = await wa.apply_statuses(
        session, [wa.StatusEvent(id="wamid.d", status="delivered", timestamp="1790000000")]
    )
    assert result == {"updated": 1, "fallbacks": 0}
    [row] = await log_rows(session)
    assert row.status == "delivered" and row.fallback_status == "none"
    assert row.delivered_at == datetime.fromtimestamp(1790000000, UTC)
    await wa.apply_statuses(session, [wa.StatusEvent(id="wamid.d", status="read")])
    await session.execute(
        update(OutboundMessage).values(fallback_deadline_at=datetime.now(UTC) - timedelta(hours=1))
    )
    assert (await wa.check_pending(session))[1]["fallbacks"] == 0 and len(http.calls) == 1
    assert (await log_rows(session))[0].read_at is not None
    await session.rollback()


async def test_webhook_failed_falls_back_for_critical_rows_only(session, http, meta_on, sms_on, factory):
    http.responder = lambda r, n: ok_wa(f"wamid.{n}") if "graph.facebook.com" in str(r.url) else sms_ok()
    await send(session)
    user = await opted_in(factory)
    await wa.send_template(
        session, template_key="new_offer", params=["a", "b"], user_id=user.id, idempotency_key="k2"
    )
    result = await wa.apply_statuses(
        session,
        [
            wa.StatusEvent(id="wamid.1", status="failed", error_code="131049"),
            wa.StatusEvent(id="wamid.2", status="failed", error_code="131049"),
            wa.StatusEvent(id="wamid.unknown", status="failed"),
        ],
    )
    assert result == {"updated": 2, "fallbacks": 1}
    rows = {r.idempotency_key: r for r in await log_rows(session)}
    assert rows["noresp:o1:c1"].error_code == "131049" and rows["noresp:o1:c1"].fallback_status == "sent"
    assert rows["k2"].status == "failed" and "k2:sms" not in rows
    await session.rollback()


async def test_fallback_action(session, http, meta_on, sms_on):
    http.responder = lambda _r, _n: ok_wa("wamid.z")
    _s, sent = await send(session)
    await session.execute(update(OutboundMessage).values(status="delivered"))
    assert (await wa.fallback(session, sent["log_id"]))[1] == {"success": True, "fallback": "not_needed"}
    assert (await wa.fallback(session, "not-a-uuid"))[0] == 404
    await session.execute(update(OutboundMessage).values(status="failed"))
    http.responder = lambda _r, _n: sms_ok()
    first = (await wa.fallback(session, sent["log_id"]))[1]
    assert first["fallback"] == "sent"
    assert (await wa.fallback(session, sent["log_id"]))[1]["fallback"] == "already"
    await session.rollback()


async def test_fallback_skipped_without_sms_text(session, http, meta_on, factory):
    http.responder = lambda _r, _n: ok_wa("wamid.n")
    user = await opted_in(factory)
    _s, sent = await wa.send_template(
        session, template_key="new_offer", params=["a", "b"], user_id=user.id, idempotency_key="n1"
    )
    assert (await wa.fallback(session, sent["log_id"]))[1] == {"success": True, "fallback": "skipped"}
    await session.rollback()


def test_extract_statuses():
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "statuses": [
                                {
                                    "id": "wamid.A",
                                    "status": "delivered",
                                    "timestamp": "1790000000",
                                    "recipient_id": "21698765432",
                                },
                                {
                                    "id": "wamid.B",
                                    "status": "failed",
                                    "errors": [{"code": 131026, "title": "Message undeliverable"}],
                                },
                                {"id": "", "status": "sent"},
                                "junk",
                            ]
                        },
                    },
                    "junk",
                ]
            },
            "junk",
        ],
    }
    events = wa.extract_statuses(payload)
    assert [e.id for e in events] == ["wamid.A", "wamid.B"]
    assert events[1].error_code == "131026" and events[1].error_message == "Message undeliverable"
    assert wa.extract_statuses({}) == [] and wa.extract_statuses([]) == []


# ─────────────────────────── webhook route ───────────────────────────


def test_signature_and_challenge_helpers():
    raw = b'{"entry":[]}'
    sig = f"sha256={hmac_sha256_hex('secret', raw)}"
    assert verify_signature(raw, sig, "secret")
    assert not verify_signature(raw, sig, "other")
    assert not verify_signature(raw + b" ", sig, "secret")
    assert not verify_signature(raw, None, "secret")
    assert not verify_signature(raw, sig, None)
    assert not verify_signature(raw, "md5=abc", "secret")
    params = {"hub.mode": "subscribe", "hub.verify_token": "tok", "hub.challenge": "42"}
    assert verify_challenge(params, "tok") == "42"
    assert verify_challenge(params, "bad") is None and verify_challenge(params, None) is None
    assert verify_challenge({**params, "hub.mode": "unsubscribe"}, "tok") is None


@pytest.mark.parametrize("path", ["/api/webhooks/whatsapp", "/api/functions/whatsappWebhook"])
async def test_webhook_get_challenge(client, monkeypatch, path):
    query = {"hub.mode": "subscribe", "hub.verify_token": "tok", "hub.challenge": "123"}
    off = await client.get(path, params=query)
    assert off.status_code == 403  # no WHATSAPP_VERIFY_TOKEN: verification refused
    monkeypatch.setattr(settings, "WHATSAPP_VERIFY_TOKEN", "tok")
    ok = await client.get(path, params=query)
    assert (ok.status_code, ok.text) == (200, "123") and ok.headers["content-type"].startswith("text/plain")
    bad = await client.get(path, params={**query, "hub.verify_token": "nope"})
    assert (bad.status_code, bad.text) == (403, "Forbidden")


@pytest.mark.parametrize("path", ["/api/webhooks/whatsapp", "/api/functions/whatsappWebhook"])
async def test_webhook_post_statuses(client, monkeypatch, path, http, meta_on, sms_on):
    async with SessionLocal() as s:
        s.add(
            OutboundMessage(
                channel="whatsapp",
                purpose="customer_no_response",
                to_e164="+21698765432",
                status="sent",
                provider_message_id="wamid.W",
                critical=True,
                idempotency_key="w1",
                params=["a", "b", "c"],
                fallback_status="pending",
            )
        )
        await s.commit()
    raw = json.dumps(
        {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "statuses": [
                                    {"id": "wamid.W", "status": "failed", "errors": [{"code": 131026}]}
                                ]
                            }
                        }
                    ]
                }
            ]
        }
    ).encode()
    unsigned = await client.post(path, content=raw)
    assert (unsigned.status_code, unsigned.text) == (
        401,
        "Invalid signature",
    )  # webhook OFF without the secret
    monkeypatch.setattr(settings, "WHATSAPP_APP_SECRET", "app-secret")
    wrong = await client.post(path, content=raw, headers={"X-Hub-Signature-256": "sha256=" + "0" * 64})
    assert wrong.status_code == 401
    http.responder = lambda _r, _n: sms_ok()
    signed = await client.post(
        path, content=raw, headers={"X-Hub-Signature-256": f"sha256={hmac_sha256_hex('app-secret', raw)}"}
    )
    assert (signed.status_code, signed.json()) == (200, {"success": True, "updated": 1, "fallbacks": 1})
    async with SessionLocal() as s:
        rows = {r.channel: r for r in (await s.execute(select(OutboundMessage))).scalars()}
    assert rows["whatsapp"].status == "failed" and rows["sms"].status == "sent"
    junk = b"not json"
    broken = await client.post(
        path, content=junk, headers={"X-Hub-Signature-256": f"sha256={hmac_sha256_hex('app-secret', junk)}"}
    )
    assert (broken.status_code, broken.json()) == (200, {"success": False})


async def test_webhook_fallback_error_is_contained(session, http, meta_on, monkeypatch):
    http.responder = lambda _r, _n: ok_wa("wamid.E")
    await send(session)

    async def broken(*_a, **_k):
        raise RuntimeError("sms down")

    monkeypatch.setattr(wa, "fallback", broken)
    result = await wa.apply_statuses(session, [wa.StatusEvent(id="wamid.E", status="failed")])
    assert result == {"updated": 1, "fallbacks": 0}
    await session.rollback()


# ─────────────────────────── sendWhatsAppMessage (admin) ───────────────────────────


async def test_send_whatsapp_message_function_is_admin_only(client, parties, http):
    url = "/api/functions/sendWhatsAppMessage"
    denied = await client.post(url, json={**ALERT}, headers=auth(parties.customer))
    assert (denied.status_code, denied.json()) == (403, {"error": "Forbidden"})
    order = await make_order(parties.customer, parties.courier)
    admin = auth(parties.admin)
    sent = await client.post(
        url,
        json={
            **ALERT,
            "to": None,
            "user_id": parties.customer.email,
            "order_id": str(order.id),
            "notification_id": "junk",
        },
        headers=admin,
    )
    assert sent.status_code == 200 and sent.json()["whatsapp"] == "disabled"
    by_id = await client.post(
        url, json={**ALERT, "idempotency_key": "x2", "user_id": str(parties.customer.id)}, headers=admin
    )
    assert by_id.json()["whatsapp"] == "disabled"
    summary = await client.post(
        url, json={"action": "summary", "idempotency_key": ALERT["idempotency_key"]}, headers=admin
    )
    assert summary.json()["whatsapp"] == "disabled" and summary.json()["sms"] == "disabled"
    pending = await client.post(
        url, json={"action": "check_pending", "order_id": str(order.id)}, headers=admin
    )
    assert pending.json() == {"success": True, "fallbacks": 0, "retried": 0}
    fb = await client.post(url, json={"action": "fallback", "log_id": sent.json()["log_id"]}, headers=admin)
    assert fb.json()["fallback"] == "already"
    unknown = await client.post(url, json={"action": "explode"}, headers=admin)
    assert (unknown.status_code, unknown.json()) == (400, {"error": "unknown action"})
    async with SessionLocal() as s:
        row = (
            (await s.execute(select(OutboundMessage).where(OutboundMessage.channel == "whatsapp")))
            .scalars()
            .first()
        )
        assert row.order_id == order.id and row.user_id == parties.customer.id and row.notification_id is None
        assert (await s.get(User, parties.customer.id)).phone_e164 == row.to_e164


# ─────────────────────────── jobs ───────────────────────────


async def test_whatsapp_check_pending_job(http, meta_on, sms_on):
    assert JOBS["whatsapp_check_pending"].trigger.interval == timedelta(minutes=5)
    async with SessionLocal() as s:
        s.add(
            OutboundMessage(
                channel="whatsapp",
                purpose="customer_no_response",
                to_e164="+21698765432",
                status="sent",
                critical=True,
                idempotency_key="j1",
                params=["a", "b", "c"],
                fallback_status="pending",
                fallback_deadline_at=datetime.now(UTC) - timedelta(seconds=5),
            )
        )
        await s.commit()
    http.responder = lambda _r, _n: sms_ok()
    assert await messaging_jobs.whatsapp_check_pending() == {"fallbacks": 1, "retried": 0}
    assert await messaging_jobs.whatsapp_check_pending() == {"fallbacks": 0, "retried": 0}


async def test_test_data_purge_runs_registered_steps(monkeypatch):
    assert JOBS["test_data_purge"].trigger.interval == timedelta(hours=1)
    monkeypatch.setattr(messaging_jobs, "PURGE_STEPS", {})
    empty = await messaging_jobs.test_data_purge()
    assert empty["steps"] == [] and empty["failed_steps"] == 0 and "total_runtime_ms" in empty

    @messaging_jobs.purge_step("hot_deals")
    async def hot_deals(_session):
        return {"expired_deals_deleted": 2, "test_run_deals_deleted": 1}

    @messaging_jobs.purge_step("broken")
    async def broken(_session):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        messaging_jobs.purge_step("hot_deals")(hot_deals)
    result = await messaging_jobs.test_data_purge()
    assert result["expired_deals_deleted"] == 2 and result["test_run_deals_deleted"] == 1
    assert result["failed_steps"] == 1 and result["steps"] == ["broken", "hot_deals"]
