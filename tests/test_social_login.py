"""« Continuer avec Google » without code nor password (owner 19/10/2026), from the website and
from the phone app (handoff through the phone's browser), and the 10-day sliding session.
Google is simulated (httpx.MockTransport): no network call."""

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from sqlalchemy import select, update

from app.config import settings
from app.db import SessionLocal
from app.integrations import google_oauth
from app.models import AuthHandoff, Courier, RefreshToken, User
from app.services import google_handoff
from tests.factories import PASSWORD, auth, error_of
from tests.order_helpers import OrderWorld

EMAIL = "g.person@example.test"


@pytest.fixture
async def world(factory):
    return await OrderWorld(factory).setup()


@pytest.fixture
def google(monkeypatch):
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "client-id.test")
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "client-secret.test")
    profile = {"sub": "sub-1", "email": EMAIL, "email_verified": True, "name": "Gina Person"}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "google-access"})
        if request.url.host == "openidconnect.googleapis.com":
            return httpx.Response(200, json=profile)
        raise AssertionError(f"unexpected call to {request.url}")

    monkeypatch.setattr(
        google_oauth, "http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    return profile


async def web_round_trip(client) -> httpx.Response:
    start = await client.get("/api/auth/google/start", params={"next": "/"})
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    return await client.get("/api/auth/google/callback", params={"code": "c", "state": state})


async def user_by_email(email: str) -> User:
    async with SessionLocal() as s:
        return (await s.execute(select(User).where(User.email == email))).scalar_one()


# --- providers --------------------------------------------------------------------------------


async def test_providers_hide_google_without_its_keys(client, monkeypatch):
    assert (await client.get("/api/auth/providers")).json() == {"google": False}
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "id")
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "secret")
    assert (await client.get("/api/auth/providers")).json() == {"google": True}


# --- linking: one account per address, never someone else's ------------------------------------


async def test_google_creates_an_account_without_code_nor_password(client, google):
    back = await web_round_trip(client)
    assert "access_token=" in back.headers["location"]
    created = await user_by_email(EMAIL)
    assert created.password_hash is None and created.email_verified_at is not None
    assert created.full_name == "Gina Person"
    me = await client.get("/api/auth/me", headers=auth(created))
    assert me.json()["google_linked"] is True and me.json()["has_password"] is False
    # the next time: same account, no second one
    await web_round_trip(client)
    async with SessionLocal() as s:
        assert len((await s.execute(select(User).where(User.email == EMAIL))).scalars().all()) == 1


async def test_google_links_a_confirmed_email_account_and_keeps_its_password(client, factory, google):
    existing = await factory.user(email=EMAIL)
    await web_round_trip(client)
    linked = await user_by_email(EMAIL)
    assert linked.id == existing.id and linked.google_sub == "sub-1"
    assert linked.password_hash is not None  # the e-mail + password sign-in still works
    login = await client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert login.status_code == 200


async def test_unconfirmed_sign_up_by_someone_else_loses_its_password(client, factory, google):
    """Pre-registration takeover: someone signed up with this address (never confirmed) and knows
    the password. Google proves the real owner: that password and its sessions are dropped."""
    squatter = await factory.user(email=EMAIL, verified=False)
    async with SessionLocal() as s:
        s.add(
            RefreshToken(
                user_id=squatter.id,
                token_hash="squatter",
                family=squatter.id,
                expires_at=datetime.now(UTC) + timedelta(days=5),
            )
        )
        await s.commit()
    await web_round_trip(client)
    owner = await user_by_email(EMAIL)
    assert owner.id == squatter.id and owner.password_hash is None and owner.email_verified_at
    login = await client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert login.status_code != 200
    async with SessionLocal() as s:
        token = (
            await s.execute(select(RefreshToken).where(RefreshToken.token_hash == "squatter"))
        ).scalar_one()
    assert token.revoked_at is not None


async def test_another_google_account_never_takes_a_linked_address(client, factory, google):
    await factory.user(email=EMAIL, google_sub="sub-other")
    back = await web_round_trip(client)
    assert "auth_error=google_account_mismatch" in back.headers["location"]


async def test_unverified_google_email_is_refused(client, google):
    google["email_verified"] = False
    back = await web_round_trip(client)
    assert "auth_error=google_email_unverified" in back.headers["location"]


# --- the phone app: handoff through the phone's browser -----------------------------------------


async def app_start(client) -> dict:
    started = await client.post("/api/auth/google/app-start")
    assert started.status_code == 200
    body = started.json()
    assert urlparse(body["authorize_url"]).netloc == "accounts.google.com"
    return body


def state_of(body: dict) -> str:
    return parse_qs(urlparse(body["authorize_url"]).query)["state"][0]


async def test_app_start_without_google_keys(client):
    r = await client.post("/api/auth/google/app-start")
    assert r.status_code == 503 and error_of(r) == "google_disabled"


async def test_app_handoff_signs_the_app_in_once(client, google):
    body = await app_start(client)
    claim = {"handoff": body["handoff"], "secret": body["secret"]}
    pending = await client.post("/api/auth/google/app-claim", json=claim)
    assert pending.status_code == 202 and pending.json() == {"status": "pending"}

    # the phone's browser: no nonce cookie (it is not the app's WebView)
    page = await client.get("/api/auth/google/callback", params={"code": "c", "state": state_of(body)})
    assert page.status_code == 200 and 'data-outcome="done"' in page.text
    assert "Revenez dans l’application ODS Delivery" in page.text and "يُرجى العودة" in page.text
    assert settings.REFRESH_COOKIE_NAME not in page.headers.get("set-cookie", "")  # no session in the browser

    wrong = await client.post("/api/auth/google/app-claim", json={**claim, "secret": "guess"})
    assert wrong.status_code == 400
    done = await client.post("/api/auth/google/app-claim", json=claim)
    assert done.status_code == 200 and done.json()["access_token"]
    assert done.json()["user"]["email"] == EMAIL
    assert settings.REFRESH_COOKIE_NAME in done.headers["set-cookie"]
    again = await client.post("/api/auth/google/app-claim", json=claim)
    assert again.status_code == 400 and error_of(again) == "handoff_expired"


async def test_app_handoff_refuses_a_browser_on_another_network(client, google):
    """A Google link sent by message to someone else cannot sign the sender's app into their account."""
    body = await app_start(client)
    async with SessionLocal() as s:
        await s.execute(update(AuthHandoff).values(client_ip="203.0.113.9"))
        await s.commit()
    page = await client.get("/api/auth/google/callback", params={"code": "c", "state": state_of(body)})
    assert 'data-outcome="handoff_other_device"' in page.text
    async with SessionLocal() as s:
        assert (await s.execute(select(User).where(User.email == EMAIL))).scalar_one_or_none() is None
    claim = await client.post(
        "/api/auth/google/app-claim", json={"handoff": body["handoff"], "secret": body["secret"]}
    )
    assert claim.status_code == 202  # still waiting, nothing was handed over


async def test_app_handoff_cancelled_and_expired(client, google):
    body = await app_start(client)
    page = await client.get(
        "/api/auth/google/callback", params={"error": "access_denied", "state": state_of(body)}
    )
    assert 'data-outcome="google_cancelled"' in page.text
    claim = {"handoff": body["handoff"], "secret": body["secret"]}
    r = await client.post("/api/auth/google/app-claim", json=claim)
    assert r.status_code == 400 and error_of(r) == "google_cancelled"

    late = await app_start(client)
    async with SessionLocal() as s:
        await s.execute(
            update(AuthHandoff)
            .where(AuthHandoff.id == late["handoff"])
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await s.commit()
    page = await client.get("/api/auth/google/callback", params={"code": "c", "state": state_of(late)})
    assert 'data-outcome="handoff_expired"' in page.text


async def test_app_handoff_puts_a_verified_courier_online(client, world, google):
    google["email"] = world.courier_user.email
    async with SessionLocal() as s:
        await s.execute(update(Courier).where(Courier.id == world.courier.id).values(is_online=False))
        await s.commit()
    body = await app_start(client)
    await client.get("/api/auth/google/callback", params={"code": "c", "state": state_of(body)})
    done = await client.post(
        "/api/auth/google/app-claim", json={"handoff": body["handoff"], "secret": body["secret"]}
    )
    assert done.status_code == 200
    async with SessionLocal() as s:
        assert (await s.get(Courier, world.courier.id)).is_online is True


def test_same_network_rule():
    assert google_handoff.same_network("41.226.1.2", "41.226.1.2")
    assert not google_handoff.same_network("41.226.1.2", "41.226.1.3")
    assert google_handoff.same_network("2a01:e0a:1:2:aaaa::1", "2a01:e0a:1:2:bbbb::9")
    assert not google_handoff.same_network("2a01:e0a:1:2::1", "2a01:e0a:1:3::1")
    assert not google_handoff.same_network("41.226.1.2", "2a01:e0a:1:2::1")
    assert not google_handoff.same_network(None, "41.226.1.2")


# --- 10-day sliding session --------------------------------------------------------------------


async def test_session_slides_ten_days_from_each_opening(client, factory):
    user = await factory.user()
    await client.post("/api/auth/login", json={"email": user.email, "password": PASSWORD})
    async with SessionLocal() as s:
        first = (await s.execute(select(RefreshToken).where(RefreshToken.user_id == user.id))).scalar_one()
    assert timedelta(days=9, hours=23) < first.expires_at - datetime.now(UTC) <= timedelta(days=10)

    # opened 9 days later: the session goes on, for 10 more days
    async with SessionLocal() as s:
        await s.execute(
            update(RefreshToken)
            .where(RefreshToken.id == first.id)
            .values(expires_at=datetime.now(UTC) + timedelta(days=1))
        )
        await s.commit()
    opened = await client.post("/api/auth/refresh")
    assert opened.status_code == 200
    assert f"max-age={10 * 86400}" in opened.headers["set-cookie"].lower()
    async with SessionLocal() as s:
        live = (
            await s.execute(
                select(RefreshToken).where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
            )
        ).scalar_one()
    assert live.expires_at - datetime.now(UTC) > timedelta(days=9, hours=23)

    # 10 days without opening the app: signed out
    async with SessionLocal() as s:
        await s.execute(
            update(RefreshToken)
            .where(RefreshToken.id == live.id)
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await s.commit()
    assert (await client.post("/api/auth/refresh")).status_code == 401


async def test_sessions_issued_before_the_change_keep_working(client, factory):
    """Deploy: a 30-day token issued yesterday still refreshes (then slides by 10 days)."""
    user = await factory.user()
    await client.post("/api/auth/login", json={"email": user.email, "password": PASSWORD})
    async with SessionLocal() as s:
        await s.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user.id)
            .values(expires_at=datetime.now(UTC) + timedelta(days=29))
        )
        await s.commit()
    assert (await client.post("/api/auth/refresh")).status_code == 200
