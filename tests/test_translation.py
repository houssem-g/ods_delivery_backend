"""Chat translation: translateOrderMessage, getTranslationStatus, the cached translation in
getOrderMessages, the language heuristic and the answer parsing. The inference endpoint is an
httpx.MockTransport (fixture `llm`): no test reaches the network."""

import json
import logging
import uuid
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import Message, MessageTranslation, TranslationUsage
from app.services import translation as tr
from tests.factories import auth
from tests.messaging_factories import make_message, make_order

SECRET_TEXT = "3aslema, win enti? ena 9odem el bab"


class LlmRecorder:
    """Stands in for the DigitalOcean inference endpoint: every call is recorded."""

    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        self.responder = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self.responder is None:
            raise AssertionError(f"unexpected network call: {request.url}")
        return self.responder(request, len(self.calls))

    def body(self, n: int = 0) -> dict:
        return json.loads(self.calls[n].content)


@pytest.fixture
def llm(monkeypatch) -> LlmRecorder:
    recorder = LlmRecorder()
    monkeypatch.setattr(
        tr, "http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(recorder.handler))
    )
    monkeypatch.setitem(tr._json_mode, "supported", True)
    monkeypatch.setitem(tr._budget_warned, "month", None)
    return recorder


@pytest.fixture
def translate_on(monkeypatch):
    monkeypatch.setattr(settings, "TRANSLATE_API_KEY", "do-model-key")
    monkeypatch.setattr(settings, "TRANSLATE_MODEL", "openai-gpt-4o-mini")
    monkeypatch.setattr(settings, "TRANSLATE_PRICE_IN_PER_M", 0.15)
    monkeypatch.setattr(settings, "TRANSLATE_PRICE_OUT_PER_M", 0.60)
    monkeypatch.setattr(settings, "TRANSLATE_MONTHLY_BUDGET_USD", 3.0)


def completion(source="arabizi", translation="Bonjour, où es-tu ? Je suis devant la porte", usage=True):
    content = json.dumps({"source": source, "translation": translation}, ensure_ascii=False)
    body = {"choices": [{"index": 0, "message": {"role": "assistant", "content": content}}]}
    if usage:
        body["usage"] = {"prompt_tokens": 400, "completion_tokens": 30, "total_tokens": 430}
    return httpx.Response(200, json=body)


async def call(client, name, user, payload):
    return await client.post(f"/api/functions/{name}", json=payload, headers=auth(user))


async def rows(model):
    async with SessionLocal() as s:
        return list((await s.execute(select(model))).scalars())


# ─────────────────────────── pure helpers ───────────────────────────


@pytest.mark.parametrize(
    ("text", "arabizi"),
    [
        ("3aslema", True),
        ("win enti", True),
        ("9odem el bab", True),
        ("n7eb nji taw", True),
        ("ya3tik sa7a", True),
        ("Bonjour, je suis devant la porte", False),
        ("2 pizzas, 10dt, dans 5min, 3eme étage", False),
        ("Rue 7 novembre", False),
        ("", False),
    ],
)
def test_arabizi_signs(text, arabizi):
    assert tr.looks_arabizi(text) is arabizi


@pytest.mark.parametrize(
    ("text", "target", "skip"),
    [
        ("Bonjour, je suis devant la porte", "fr", True),
        ("Bonjour, je suis devant la porte", "ar", False),
        ("أنا قدام الباب", "ar", True),
        ("وينك؟ انا قدام الباب", "fr", False),
        ("3aslema win enti", "fr", False),
        ("3aslema win enti", "ar", False),
        ("انا توا في Carrefour نشري", "ar", True),
        ("انا في Carrefour", "ar", False),
        ("انا توا, taw nji", "ar", False),
        ("👍 5", "fr", True),
        ("👍", "ar", True),
    ],
)
def test_already_in_target(text, target, skip):
    assert tr.already_in(text, target) is skip


def test_parse_answer_robustness():
    good = '{"source": "derja", "translation": "Je suis là"}'
    assert tr.parse_answer(good) == ("derja", "Je suis là")
    assert tr.parse_answer(f"```json\n{good}\n```") == ("derja", "Je suis là")
    assert tr.parse_answer(f"Voici la traduction : {good} !") == ("derja", "Je suis là")
    assert tr.parse_answer([{"type": "text", "text": good}]) == ("derja", "Je suis là")
    assert tr.parse_answer('{"source": "Tunisian Arabic", "translation": " ok "}') == ("derja", "ok")
    assert tr.parse_answer('{"source": "klingon", "translation": "x"}') == ("other", "x")
    assert tr.parse_answer('{"source_lang": "fr", "text": "salut"}') == ("fr", "salut")
    assert tr.parse_answer("Je suis devant la porte") == ("other", "Je suis devant la porte")
    for bad in (
        None,
        "",
        "   ",
        5,
        '{"source": "fr"}',
        '{"translation": ""}',
        '{"source": "fr", "tr',
        "[1, 2]",
    ):
        assert tr.parse_answer(bad) is None, bad
    assert len(tr.parse_answer(json.dumps({"translation": "x" * 9000}))[1]) == tr.MAX_TRANSLATION


def test_cost_and_request_shape(monkeypatch):
    # prices pinned here: the defaults follow the production model
    monkeypatch.setattr(settings, "TRANSLATE_PRICE_IN_PER_M", 0.15)
    monkeypatch.setattr(settings, "TRANSLATE_PRICE_OUT_PER_M", 0.60)
    assert tr.cost_of(1_000_000, 1_000_000) == Decimal("0.750000")
    assert tr.cost_of(400, 30) == Decimal("0.000078")
    body = tr._request_body("salut", "ar", json_mode=True)
    assert body["response_format"] == {"type": "json_object"} and body["temperature"] == 0
    assert body["max_tokens"] == tr.MAX_OUTPUT_TOKENS
    assert json.loads(body["messages"][1]["content"]) == {"target": "ar", "message": "salut"}
    monkeypatch.setattr(settings, "TRANSLATE_MODEL", "openai-gpt-5-nano")
    reasoning = tr._request_body("salut", "fr", json_mode=False)
    assert "temperature" not in reasoning and "response_format" not in reasoning
    assert reasoning["max_completion_tokens"] == tr.MAX_OUTPUT_TOKENS_REASONING


# ─────────────────────────── translateOrderMessage ───────────────────────────


async def test_disabled_without_a_key(client, parties, llm):
    order = await make_order(parties.customer, parties.courier)
    msg = await make_message(order, parties.customer, "customer", parties.courier_user, body=SECRET_TEXT)
    res = await call(client, "translateOrderMessage", parties.courier_user, {"message_id": str(msg.id)})
    assert res.status_code == 200
    assert res.json() == {
        "success": True,
        "message_id": str(msg.id),
        "available": False,
        "reason": "disabled",
        "target": "ar",
        "source_lang": None,
        "translation": None,
        "cached": False,
    }
    assert llm.calls == [] and await rows(MessageTranslation) == [] and await rows(TranslationUsage) == []


async def test_translates_once_then_answers_from_the_cache(client, parties, llm, translate_on, caplog):
    order = await make_order(parties.customer, parties.courier)
    msg = await make_message(order, parties.customer, "customer", parties.courier_user, body=SECRET_TEXT)
    llm.responder = lambda _r, _n: completion()
    caplog.set_level(logging.DEBUG)
    payload = {"message_id": str(msg.id), "target": "fr"}
    first = await call(client, "translateOrderMessage", parties.courier_user, payload)
    assert first.status_code == 200
    assert first.json() == {
        "success": True,
        "message_id": str(msg.id),
        "available": True,
        "target": "fr",
        "source_lang": "arabizi",
        "translation": "Bonjour, où es-tu ? Je suis devant la porte",
        "cached": False,
    }
    [request] = llm.calls
    assert str(request.url) == "https://inference.do-ai.run/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer do-model-key"
    sent = llm.body()
    assert sent["model"] == "openai-gpt-4o-mini" and sent["response_format"] == {"type": "json_object"}
    assert json.loads(sent["messages"][1]["content"]) == {"target": "fr", "message": SECRET_TEXT}

    [row] = await rows(MessageTranslation)
    assert (row.message_id, row.target_lang, row.source_lang) == (msg.id, "fr", "arabizi")
    assert (row.model, row.input_tokens, row.output_tokens) == ("openai-gpt-4o-mini", 400, 30)
    assert row.cost_usd == Decimal("0.000078")
    [usage] = await rows(TranslationUsage)
    assert (usage.calls, usage.input_tokens, usage.output_tokens) == (1, 400, 30)
    assert usage.cost_usd == Decimal("0.000078") and usage.month == tr.month_start()

    # the customer too can read it, from the cache: no second call
    again = await call(client, "translateOrderMessage", parties.customer, payload)
    assert again.json()["cached"] is True and again.json()["translation"].startswith("Bonjour")
    assert len(llm.calls) == 1
    # another language is another call
    await call(
        client, "translateOrderMessage", parties.courier_user, {"message_id": str(msg.id), "target": "ar"}
    )
    assert len(llm.calls) == 2
    [usage] = await rows(TranslationUsage)
    assert usage.calls == 2 and usage.cost_usd == Decimal("0.000156")
    assert SECRET_TEXT not in caplog.text and "3aslema" not in caplog.text


async def test_default_target_is_the_callers_language(client, parties, factory, llm, translate_on):
    french = await factory.user(email="fr@example.test", role="courier", language="fr")
    courier = await factory.courier(french, verification="verified", phone_e164="+21622000222")
    order = await make_order(parties.customer, courier)
    msg = await make_message(order, parties.customer, "customer", french, body="وينك؟ انا قدام الباب")
    llm.responder = lambda _r, _n: completion("derja", "Où es-tu ? Je suis devant la porte")
    res = await call(client, "translateOrderMessage", french, {"message_id": str(msg.id)})
    assert res.json()["target"] == "fr" and res.json()["source_lang"] == "derja"
    assert json.loads(llm.body()["messages"][1]["content"])["target"] == "fr"


async def test_no_call_when_nothing_to_translate(client, parties, llm, translate_on):
    order = await make_order(parties.customer, parties.courier)
    french = await make_message(order, parties.customer, "customer", body="Je suis devant la porte")
    res = await call(
        client, "translateOrderMessage", parties.courier_user, {"message_id": str(french.id), "target": "fr"}
    )
    body = res.json()
    assert body["available"] is True and body["source_lang"] == "fr" and body["translation"] is None
    async with SessionLocal() as s:
        photo = Message(
            order_id=order.id, sender_id=parties.customer.id, sender_role="customer", body="",
            attachment_key="private/chat/x.jpg", attachment_type="image",
        )  # fmt: skip
        s.add(photo)
        await s.commit()
    res = await call(
        client, "translateOrderMessage", parties.courier_user, {"message_id": str(photo.id), "target": "fr"}
    )
    body = res.json()
    assert body["available"] is True and body["translation"] is None and body["source_lang"] is None
    assert llm.calls == [] and await rows(MessageTranslation) == []


async def test_model_says_already_in_the_target_language(client, parties, llm, translate_on):
    order = await make_order(parties.customer, parties.courier)
    msg = await make_message(order, parties.customer, "customer", body="ok 3la 5 dt")
    llm.responder = lambda _r, _n: completion("fr", "ok 3la 5 dt")
    res = await call(
        client, "translateOrderMessage", parties.courier_user, {"message_id": str(msg.id), "target": "fr"}
    )
    assert res.json()["available"] is True and res.json()["translation"] is None
    [row] = await rows(MessageTranslation)
    assert row.translated_text is None and row.source_lang == "fr"
    again = await call(
        client, "translateOrderMessage", parties.courier_user, {"message_id": str(msg.id), "target": "fr"}
    )
    assert again.json()["cached"] is True and len(llm.calls) == 1


async def test_budget_stops_new_calls_but_not_the_cache(client, parties, llm, translate_on, caplog):
    order = await make_order(parties.customer, parties.courier)
    cached = await make_message(order, parties.customer, "customer", body=SECRET_TEXT)
    fresh = await make_message(order, parties.customer, "customer", body="chnowa l7kaya")
    async with SessionLocal() as s:
        s.add(
            TranslationUsage(
                month=tr.month_start(),
                calls=900,
                input_tokens=1,
                output_tokens=1,
                cost_usd=Decimal("3.000001"),
            )
        )
        s.add(
            MessageTranslation(
                message_id=cached.id,
                target_lang="fr",
                translated_text="Salut",
                source_lang="arabizi",
                model="m",
            )
        )
        await s.commit()
    caplog.set_level(logging.WARNING, logger="odsd.translation")
    for _ in range(2):
        res = await call(
            client,
            "translateOrderMessage",
            parties.courier_user,
            {"message_id": str(fresh.id), "target": "fr"},
        )
        assert res.status_code == 200
        assert res.json()["available"] is False and res.json()["reason"] == "budget"
    assert [r.message for r in caplog.records].count(caplog.records[0].message) == 1
    assert "budget reached" in caplog.records[0].message and len(caplog.records) == 1
    hit = await call(
        client, "translateOrderMessage", parties.courier_user, {"message_id": str(cached.id), "target": "fr"}
    )
    assert hit.json()["translation"] == "Salut" and hit.json()["cached"] is True
    assert llm.calls == []


async def test_timeout_is_retried_once_then_unavailable(client, parties, llm, translate_on):
    order = await make_order(parties.customer, parties.courier)
    msg = await make_message(order, parties.customer, "customer", body=SECRET_TEXT)

    def timeout(request, _n):
        raise httpx.ReadTimeout("slow", request=request)

    llm.responder = timeout
    res = await call(
        client, "translateOrderMessage", parties.courier_user, {"message_id": str(msg.id), "target": "fr"}
    )
    assert res.status_code == 200
    assert res.json()["available"] is False and res.json()["reason"] == "unavailable"
    assert len(llm.calls) == 2
    assert await rows(MessageTranslation) == [] and await rows(TranslationUsage) == []


async def test_server_error_retried_once_then_translated(client, parties, llm, translate_on):
    order = await make_order(parties.customer, parties.courier)
    msg = await make_message(order, parties.customer, "customer", body=SECRET_TEXT)
    llm.responder = lambda _r, n: httpx.Response(503, text="busy") if n == 1 else completion()
    res = await call(
        client, "translateOrderMessage", parties.courier_user, {"message_id": str(msg.id), "target": "fr"}
    )
    assert res.json()["available"] is True and len(llm.calls) == 2


@pytest.mark.parametrize("status", [401, 402, 429])
async def test_refusals_are_not_retried(client, parties, llm, translate_on, status):
    order = await make_order(parties.customer, parties.courier)
    msg = await make_message(order, parties.customer, "customer", body=SECRET_TEXT)
    llm.responder = lambda _r, _n: httpx.Response(status, json={"error": "insufficient balance"})
    res = await call(
        client, "translateOrderMessage", parties.courier_user, {"message_id": str(msg.id), "target": "fr"}
    )
    assert res.status_code == 200 and res.json()["reason"] == "unavailable" and len(llm.calls) == 1


async def test_unreadable_answer_is_paid_but_not_cached(client, parties, llm, translate_on):
    order = await make_order(parties.customer, parties.courier)
    msg = await make_message(order, parties.customer, "customer", body=SECRET_TEXT)
    truncated = {"choices": [{"message": {"content": '{"source": "arabizi", "translation": "Bonj'}}]}
    llm.responder = lambda _r, _n: httpx.Response(200, json=truncated)
    res = await call(
        client, "translateOrderMessage", parties.courier_user, {"message_id": str(msg.id), "target": "fr"}
    )
    assert res.json()["available"] is False and res.json()["reason"] == "unavailable"
    assert await rows(MessageTranslation) == []
    [usage] = await rows(TranslationUsage)  # no usage in the answer: estimated, still counted
    assert usage.calls == 1 and usage.input_tokens > 0 and usage.cost_usd > 0


async def test_endpoint_without_json_mode(client, parties, llm, translate_on):
    order = await make_order(parties.customer, parties.courier)
    msg = await make_message(order, parties.customer, "customer", body=SECRET_TEXT)

    def responder(request, _n):
        if "response_format" in json.loads(request.content):
            return httpx.Response(400, json={"error": {"message": "response_format is not supported"}})
        return completion()

    llm.responder = responder
    res = await call(
        client, "translateOrderMessage", parties.courier_user, {"message_id": str(msg.id), "target": "fr"}
    )
    assert res.json()["available"] is True and len(llm.calls) == 2
    assert tr._json_mode["supported"] is False


async def test_access_rules_and_validation(client, parties, factory, llm, translate_on):
    order = await make_order(parties.customer, parties.courier)
    msg = await make_message(order, parties.customer, "customer", body="Bonjour")
    cases = [
        ({}, 400, {"error": "invalid_message_id"}),
        ({"message_id": "nope"}, 400, {"error": "invalid_message_id"}),
        (
            {"message_id": str(msg.id), "target": "en"},
            400,
            {"error": "invalid_target", "allowed": ["fr", "ar"]},
        ),
        ({"message_id": str(uuid.uuid4())}, 404, {"error": "message_not_found"}),
    ]
    for payload, status, expected in cases:
        res = await call(client, "translateOrderMessage", parties.customer, payload)
        assert (res.status_code, res.json()) == (status, expected), payload
    stranger = await call(client, "translateOrderMessage", parties.stranger, {"message_id": str(msg.id)})
    assert (stranger.status_code, stranger.json()) == (403, {"error": "not_a_party"})
    admin = await call(
        client, "translateOrderMessage", parties.admin, {"message_id": str(msg.id), "target": "fr"}
    )
    assert admin.status_code == 200
    anonymous = await client.post("/api/functions/translateOrderMessage", json={"message_id": str(msg.id)})
    assert anonymous.status_code == 401

    # a courier asking on an open order sees his own messages and the customer's, not another courier's
    open_order = await make_order(parties.customer)
    other_user = await factory.user(role="courier")
    await factory.courier(other_user, verification="verified", phone_e164="+21622000333")
    theirs = await make_message(open_order, other_user, "courier", body="Bonjour")
    customers = await make_message(open_order, parties.customer, "customer", body="Bonjour")
    hidden = await call(
        client, "translateOrderMessage", parties.courier_user, {"message_id": str(theirs.id), "target": "fr"}
    )
    assert (hidden.status_code, hidden.json()) == (404, {"error": "message_not_found"})
    seen = await call(
        client,
        "translateOrderMessage",
        parties.courier_user,
        {"message_id": str(customers.id), "target": "fr"},
    )
    assert seen.status_code == 200
    assert llm.calls == []


async def test_rate_limited_per_user(client, parties, llm, monkeypatch):
    monkeypatch.setattr(settings, "RATE_LIMIT_TRANSLATE", "2/minute")
    order = await make_order(parties.customer, parties.courier)
    msg = await make_message(order, parties.customer, "customer", body="Bonjour")
    for _ in range(2):
        assert (
            await call(client, "translateOrderMessage", parties.courier_user, {"message_id": str(msg.id)})
        ).status_code == 200
    res = await call(client, "translateOrderMessage", parties.courier_user, {"message_id": str(msg.id)})
    assert (res.status_code, res.json()) == (
        429,
        {"error": "too_many_translate_requests", "limit": "2/minute"},
    )
    assert "rate limit" not in res.text.lower()
    # another user has his own counter
    assert (
        await call(client, "translateOrderMessage", parties.customer, {"message_id": str(msg.id)})
    ).status_code == 200


# ─────────────────────────── getOrderMessages / getTranslationStatus ───────────────────────────


async def test_get_order_messages_carries_the_cached_translation(client, parties, factory, llm, translate_on):
    order = await make_order(parties.customer, parties.courier)
    first = await make_message(order, parties.customer, "customer", body=SECRET_TEXT)
    second = await make_message(order, parties.courier_user, "courier", body="J'arrive")
    same = await make_message(order, parties.customer, "customer", body="ok")
    async with SessionLocal() as s:

        def row(message, lang, text, source):
            return MessageTranslation(
                message_id=message.id, target_lang=lang, translated_text=text, source_lang=source, model="m"
            )

        s.add_all(
            [
                row(first, "ar", "عسلامة، وينك؟", "arabizi"),
                row(first, "fr", "Salut, où es-tu ?", "arabizi"),
                row(same, "ar", None, "ar"),
            ]
        )
        await s.commit()
    res = await call(client, "getOrderMessages", parties.courier_user, {"order_id": str(order.id)})
    by_id = {m["id"]: m for m in res.json()["messages"]}
    # the courier's language is 'ar' (default)
    assert by_id[str(first.id)]["translation"] == {
        "target": "ar",
        "text": "عسلامة، وينك؟",
        "source_lang": "arabizi",
    }
    assert by_id[str(second.id)]["translation"] is None and by_id[str(same.id)]["translation"] is None
    reader = await factory.user(email="fr-admin@example.test", role="admin", language="fr")
    res = await call(client, "getOrderMessages", reader, {"order_id": str(order.id), "mark_read": True})
    by_id = {m["id"]: m for m in res.json()["messages"]}
    assert by_id[str(first.id)]["translation"]["text"] == "Salut, où es-tu ?"
    assert llm.calls == []


async def test_translation_status_for_admins(client, parties, translate_on):
    refused = await call(client, "getTranslationStatus", parties.customer, {})
    assert (refused.status_code, refused.json()) == (403, {"error": "Forbidden"})
    empty = await call(client, "getTranslationStatus", parties.admin, {})
    assert empty.json() == {
        "success": True,
        "enabled": True,
        "model": "openai-gpt-4o-mini",
        "month": tr.month_start().isoformat()[:7],
        "month_cost_usd": 0.0,
        "budget_usd": 3.0,
        "calls": 0,
    }
    async with SessionLocal() as s:
        s.add(
            TranslationUsage(
                month=tr.month_start(),
                calls=12,
                input_tokens=5000,
                output_tokens=400,
                cost_usd=Decimal("0.001234"),
            )
        )
        await s.commit()
    status = (await call(client, "getTranslationStatus", parties.admin, {})).json()
    assert status["calls"] == 12 and status["month_cost_usd"] == 0.001234


async def test_translation_status_disabled(client, parties):
    status = (await call(client, "getTranslationStatus", parties.admin, {})).json()
    assert status["enabled"] is False
