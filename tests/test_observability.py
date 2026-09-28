"""Request id, access log, metrics endpoint, log / Sentry redaction."""

import json
import logging
import re
import uuid

from apscheduler.triggers.interval import IntervalTrigger

from app.config import settings
from app.jobs.registry import Job
from app.jobs.scheduler import run_job
from app.observability import metrics
from app.observability.context import request_id_var, user_hash
from app.observability.logs import JsonFormatter, RequestIdFilter, TextFormatter
from app.observability.redact import redact
from app.observability.sentry import scrub_event
from app.security.tokens import create_access_token
from tests.factories import auth, token_for

METRICS_TOKEN = "test-metrics-token-0123456789"


# --- request id -----------------------------------------------------------------------


async def test_request_id_is_generated_when_absent(client):
    response = await client.get("/api/health")
    assert re.fullmatch(r"[0-9a-f]{32}", response.headers["x-request-id"])


async def test_a_safe_incoming_request_id_is_kept(client):
    response = await client.get("/api/health", headers={"X-Request-ID": "lb-7f3a9c21.req:42"})
    assert response.headers["x-request-id"] == "lb-7f3a9c21.req:42"


async def test_an_unsafe_incoming_request_id_is_replaced(client):
    for bad in ("short", "has spaces in it", "x" * 200, 'inject"quote"-123', "<script>alert(1)</script>"):
        response = await client.get("/api/health", headers={"X-Request-ID": bad})
        assert response.headers["x-request-id"] != bad
        assert re.fullmatch(r"[0-9a-f]{32}", response.headers["x-request-id"])


async def test_request_id_is_on_error_responses_and_exposed_to_the_browser(client):
    response = await client.get(
        "/api/entities/Order", headers={"Origin": "http://localhost:5191", "X-Request-ID": "abcdef123456"}
    )
    assert response.status_code == 401
    assert response.headers["x-request-id"] == "abcdef123456"
    assert "x-request-id" in response.headers["access-control-expose-headers"].lower()


# --- access log -------------------------------------------------------------------------


async def test_access_log_has_the_route_template_and_a_user_hash_only(client, factory, caplog):
    user = await factory.user(email="access-log@example.test")
    token = token_for(user)
    doc_id = uuid.uuid4()
    caplog.set_level(logging.INFO, logger="odsd.access")
    await client.get(f"/api/entities/Order/{doc_id}?secret=hunter2", headers=auth(user))

    records = [r for r in caplog.records if r.name == "odsd.access"]
    assert len(records) == 1
    record = records[0]
    assert record.route == "/api/entities/{name}/{doc_id}"
    assert record.method == "GET"
    assert record.user == user_hash(str(user.id), settings.JWT_SECRET)
    rendered = TextFormatter().format(record) + JsonFormatter().format(record)
    for leaked in (str(doc_id), "hunter2", token, "access-log@example.test", str(user.id)):
        assert leaked not in rendered


async def test_health_probes_are_not_logged_at_info(client, caplog):
    caplog.set_level(logging.INFO, logger="odsd.access")
    await client.get("/api/health")
    assert [r for r in caplog.records if r.name == "odsd.access"] == []


# --- metrics ----------------------------------------------------------------------------


async def test_metrics_are_off_without_a_token(client, monkeypatch):
    monkeypatch.setattr(settings, "METRICS_TOKEN", None)
    response = await client.get("/api/metrics")
    assert response.status_code == 404


async def test_metrics_refuse_a_wrong_or_missing_token(client, monkeypatch):
    monkeypatch.setattr(settings, "METRICS_TOKEN", METRICS_TOKEN)
    assert (await client.get("/api/metrics")).status_code == 401
    assert (await client.get("/api/metrics", headers={"X-Metrics-Token": "nope"})).status_code == 401


async def test_metrics_expose_route_templates_not_raw_paths(client, monkeypatch):
    monkeypatch.setattr(settings, "METRICS_TOKEN", METRICS_TOKEN)
    doc_id = uuid.uuid4()
    await client.get(f"/api/entities/Order/{doc_id}")
    await client.get("/definitely/not/a/route")

    for headers in ({"X-Metrics-Token": METRICS_TOKEN}, {"Authorization": f"Bearer {METRICS_TOKEN}"}):
        response = await client.get("/api/metrics", headers=headers)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")
    body = response.text
    assert 'odsd_http_requests_total{method="GET",route="/api/entities/{name}/{doc_id}",status="401"}' in body
    assert 'route="<unmatched>",status="404"' in body
    assert str(doc_id) not in body
    assert "odsd_http_request_duration_seconds_bucket" in body
    assert "odsd_ws_connections" in body
    assert "odsd_db_pool_checked_out" in body


async def test_job_runs_and_failures_are_counted():
    async def ok():
        return {}

    async def boom():
        raise RuntimeError("fails")

    trigger = IntervalTrigger(hours=1)
    runs = metrics.JOB_RUNS.labels("obs_boom")._value.get()
    failures = metrics.JOB_FAILURES.labels("obs_boom")._value.get()
    await run_job(Job(name="obs_ok", func=ok, trigger=trigger, description=""))
    result = await run_job(Job(name="obs_boom", func=boom, trigger=trigger, description=""))
    assert result["ok"] is False
    assert metrics.JOB_RUNS.labels("obs_boom")._value.get() == runs + 1
    assert metrics.JOB_FAILURES.labels("obs_boom")._value.get() == failures + 1
    assert metrics.JOB_FAILURES.labels("obs_ok")._value.get() == 0


# --- redaction ----------------------------------------------------------------------------


def test_redact_removes_tokens_secrets_emails_and_phones():
    jwt = create_access_token(uuid.uuid4(), "customer")[0]
    samples = {
        f"Authorization: Bearer {jwt}": jwt,
        f"GET /api/ws?token={jwt}&x=1": jwt,
        "reset for Jane.Doe+test@Example.com failed": "Jane.Doe+test@Example.com",
        'payload {"password": "Sup3r-Secret!", "email_ok": 1}': "Sup3r-Secret!",
        "verify code=482913 attempt 2": "482913",
        "sms to +216 22 123 456 queued": "22 123 456",
        "whatsapp to 0041791234567": "0041791234567",
        "call 22123456 now": "22123456",
        "client_secret=abc123def&grant=x": "abc123def",
    }
    for text, secret in samples.items():
        assert secret not in redact(text), text


def test_redact_keeps_ordinary_log_content():
    for text in (
        "job sweep done in 1234 ms at 2026-09-28 11:37:14",
        "order 6f1c2b8e-2f4d-4c55-9a1e-3b7d2c9e0a11 status_code=200",
        'status {"status_code": 409, "error": "conflict"}',
        "pool 5/10 overflow -3",
    ):
        assert redact(text) == text


def test_json_log_lines_are_redacted_and_carry_the_request_id():
    record = logging.LogRecord("odsd.test", logging.WARNING, __file__, 1, "login %s", ("x@y.tn",), None)
    reset = request_id_var.set("rid-12345678")
    try:
        RequestIdFilter().filter(record)
    finally:
        request_id_var.reset(reset)
    line = json.loads(JsonFormatter().format(record))
    assert line["message"] == "login [email]"
    assert line["request_id"] == "rid-12345678"
    assert line["level"] == "WARNING"


def test_sentry_events_are_scrubbed():
    event = {
        "request": {
            "url": "http://api.local/api/ws?token=abc.def.ghi",
            "query_string": "token=eyJabcdefgh.eyJabcdefgh.sig&x=1",
            "headers": {"Authorization": "Bearer abc", "Cookie": "odsd_refresh=r", "User-Agent": "UA"},
            "cookies": {"odsd_refresh": "r"},
            "data": {"password": "p"},
        },
        "user": {"id": "42", "email": "who@ods.tn", "ip_address": "1.2.3.4"},
        "exception": {"values": [{"value": "no user for who@ods.tn"}]},
        "extra": {"token": "t0k3n"},
    }
    reset = request_id_var.set("rid-sentry-1")
    try:
        out = json.dumps(scrub_event(event, "key"))
    finally:
        request_id_var.reset(reset)
    for leaked in ("who@ods.tn", "Bearer abc", "odsd_refresh=r", "eyJabcdefgh", "t0k3n", "1.2.3.4", '"p"'):
        assert leaked not in out
    assert "rid-sentry-1" in out
    assert event["user"] == {"id": user_hash("42", "key")}
