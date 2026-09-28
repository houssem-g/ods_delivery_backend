import re
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from sqlalchemy import select, update

from app.config import settings
from app.db import SessionLocal
from app.integrations import google_oauth
from app.models import EmailCode, RefreshToken, User
from app.services import email as email_service
from tests.factories import PASSWORD, auth, error_of

NEW_PASSWORD = "Another-Secret-7"


def last_code(to: str) -> str:
    mails = [m for m in email_service.OUTBOX if m.to == to]
    assert mails, f"no e-mail sent to {to}"
    return re.search(r"\b(\d{6})\b", mails[-1].text).group(1)


def last_link_token(to: str) -> str:
    mails = [m for m in email_service.OUTBOX if m.to == to]
    return parse_qs(urlparse(re.search(r"(http\S+reset_token=\S+)", mails[-1].text).group(1)).query)[
        "reset_token"
    ][0]


async def age_codes(email: str, seconds: int = 120) -> None:
    """Pretend the last code was sent a while ago (resend throttle)."""
    async with SessionLocal() as s:
        user = (await s.execute(select(User).where(User.email == email))).scalar_one()
        await s.execute(
            update(EmailCode)
            .where(EmailCode.user_id == user.id)
            .values(created_at=datetime.now(UTC) - timedelta(seconds=seconds))
        )
        await s.commit()


def use_refresh_cookie(client: httpx.AsyncClient, value: str) -> None:
    """Replace the jar's refresh cookie (what another tab / a replay would send)."""
    client.cookies.clear()
    client.cookies.set(settings.REFRESH_COOKIE_NAME, value)


async def login(client, email: str, password: str = PASSWORD) -> httpx.Response:
    return await client.post("/api/auth/login", json={"email": email, "password": password})


# --- register / verify / login ---------------------------------------------------------


async def test_register_verify_then_login(client):
    email = "new.customer@example.test"
    response = await client.post(
        "/api/auth/register", json={"email": email, "password": PASSWORD, "full_name": "New Customer"}
    )
    assert response.status_code == 201

    blocked = await login(client, email)
    assert blocked.status_code == 403
    assert error_of(blocked) == "email_not_verified"
    assert re.search(r"verif|confirm", blocked.json()["message"], re.I)

    wrong = await client.post("/api/auth/verify-otp", json={"email": email, "otp_code": "000000"})
    assert wrong.status_code == 400 and error_of(wrong) == "invalid_code"

    verified = await client.post("/api/auth/verify-otp", json={"email": email, "otp_code": last_code(email)})
    assert verified.status_code == 200
    assert verified.json()["user"]["is_verified"] is True

    signed_in = await login(client, email)
    assert signed_in.status_code == 200
    body = signed_in.json()
    assert body["access_token"] and body["user"]["email"] == email
    assert body["user"]["role"] == "user"
    assert settings.REFRESH_COOKIE_NAME in signed_in.headers["set-cookie"]
    assert "httponly" in signed_in.headers["set-cookie"].lower()
    assert "path=/api/auth" in signed_in.headers["set-cookie"].lower()


async def test_register_existing_verified_account(client, factory):
    user = await factory.user()
    response = await client.post(
        "/api/auth/register", json={"email": user.email.upper(), "password": PASSWORD, "full_name": "X"}
    )
    assert response.status_code == 409
    assert error_of(response) == "email_taken"
    assert "already" in response.json()["message"]


async def test_register_again_while_unverified_replaces_password(client, factory):
    user = await factory.user(verified=False)
    response = await client.post(
        "/api/auth/register", json={"email": user.email, "password": NEW_PASSWORD, "full_name": "Again"}
    )
    assert response.status_code == 201
    await client.post("/api/auth/verify-otp", json={"email": user.email, "code": last_code(user.email)})
    assert (await login(client, user.email, NEW_PASSWORD)).status_code == 200


async def test_register_weak_password(client):
    response = await client.post(
        "/api/auth/register", json={"email": "weak@example.test", "password": "short", "full_name": "W"}
    )
    assert response.status_code == 400
    assert re.search(r"password", response.json()["message"], re.I)
    assert re.search(r"short|least", response.json()["message"], re.I)


async def test_login_failures(client, factory):
    user = await factory.user()
    disabled = await factory.user(disabled_at=datetime.now(UTC))
    assert error_of(await login(client, user.email, "wrong-password")) == "invalid_credentials"
    unknown = await login(client, "nobody@example.test")
    assert unknown.status_code == 401 and error_of(unknown) == "invalid_credentials"
    refused = await login(client, disabled.email)
    assert refused.status_code == 403 and error_of(refused) == "account_disabled"


# --- account setup (accounts imported from Base44 without a password) --------------------


async def test_account_setup_flow(client, factory):
    user = await factory.user(password=None, verified=False)
    first = await login(client, user.email, "anything-at-all")
    assert first.status_code == 409 and error_of(first) == "account_setup_required"
    code = last_code(user.email)
    assert "Bienvenue" in email_service.OUTBOX[-1].subject or "مرحباً" in email_service.OUTBOX[-1].subject

    # a second attempt right away answers the same without a new e-mail (throttled)
    again = await login(client, user.email, "anything-at-all")
    assert again.status_code == 409 and len(email_service.OUTBOX) == 1

    wrong = await client.post(
        "/api/auth/account-setup", json={"email": user.email, "code": "111111", "password": NEW_PASSWORD}
    )
    assert wrong.status_code == 400 and error_of(wrong) == "invalid_code"

    done = await client.post(
        "/api/auth/account-setup", json={"email": user.email, "code": code, "password": NEW_PASSWORD}
    )
    assert done.status_code == 200
    assert done.json()["access_token"] and done.json()["user"]["is_verified"] is True
    assert settings.REFRESH_COOKIE_NAME in done.headers["set-cookie"]
    assert (await login(client, user.email, NEW_PASSWORD)).status_code == 200


async def test_register_and_resend_on_imported_account_send_the_setup_code(client, factory):
    user = await factory.user(password=None)
    response = await client.post(
        "/api/auth/register", json={"email": user.email, "password": PASSWORD, "full_name": "Me"}
    )
    assert response.status_code == 409 and error_of(response) == "account_setup_required"
    first_code = last_code(user.email)

    await age_codes(user.email)
    resent = await client.post("/api/auth/resend-otp", json={"email": user.email})
    assert resent.status_code == 200
    assert len(email_service.OUTBOX) == 2
    second_code = last_code(user.email)

    stale = await client.post(
        "/api/auth/account-setup", json={"email": user.email, "code": first_code, "password": NEW_PASSWORD}
    )
    if first_code != second_code:
        assert stale.status_code == 400  # the resend voided the older code


async def test_code_dies_after_five_wrong_attempts(client, factory):
    user = await factory.user(password=None)
    await login(client, user.email, "x-x-x-x-x-x")
    code = last_code(user.email)
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(5):
        response = await client.post(
            "/api/auth/account-setup", json={"email": user.email, "code": wrong, "password": NEW_PASSWORD}
        )
        assert response.status_code == 400
    late = await client.post(
        "/api/auth/account-setup", json={"email": user.email, "code": code, "password": NEW_PASSWORD}
    )
    assert late.status_code == 400


# --- anti-enumeration ---------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/api/auth/resend-otp", "/api/auth/reset-password-request"])
async def test_same_answer_whether_the_account_exists(client, factory, path):
    user = await factory.user(verified=False)
    known = await client.post(path, json={"email": user.email})
    unknown = await client.post(path, json={"email": "ghost@example.test"})
    assert known.status_code == unknown.status_code == 200
    assert known.json() == unknown.json()
    assert all(m.to != "ghost@example.test" for m in email_service.OUTBOX)
    assert any(m.to == user.email for m in email_service.OUTBOX)


async def test_resend_to_verified_account_sends_nothing(client, factory):
    user = await factory.user()
    response = await client.post("/api/auth/resend-otp", json={"email": user.email})
    assert response.status_code == 200 and email_service.OUTBOX == []


# --- reset password -------------------------------------------------------------------------


async def test_reset_password_with_email_and_code(client, factory):
    user = await factory.user()
    old_session = await login(client, user.email)
    old_cookie = old_session.cookies[settings.REFRESH_COOKIE_NAME]

    assert (
        await client.post("/api/auth/reset-password-request", json={"email": user.email})
    ).status_code == 200
    code = last_code(user.email)
    no_email = await client.post(
        "/api/auth/reset-password", json={"reset_token": code, "new_password": NEW_PASSWORD}
    )
    assert no_email.status_code == 400

    done = await client.post(
        "/api/auth/reset-password",
        json={"email": user.email, "reset_token": code, "new_password": NEW_PASSWORD},
    )
    assert done.status_code == 200
    assert (await login(client, user.email)).status_code == 401
    assert (await login(client, user.email, NEW_PASSWORD)).status_code == 200

    use_refresh_cookie(client, old_cookie)
    stale = await client.post("/api/auth/refresh")
    assert stale.status_code == 401  # every session was revoked by the reset


async def test_reset_password_from_the_emailed_link(client, factory):
    user = await factory.user()
    await client.post("/api/auth/reset-password-request", json={"email": user.email})
    token = last_link_token(user.email)
    done = await client.post(
        "/api/auth/reset-password", json={"reset_token": token, "new_password": NEW_PASSWORD}
    )
    assert done.status_code == 200
    reused = await client.post(
        "/api/auth/reset-password", json={"reset_token": token, "new_password": PASSWORD}
    )
    assert reused.status_code == 400


# --- change password ------------------------------------------------------------------------


async def test_change_password(client, factory):
    user = await factory.user()
    wrong = await client.post(
        "/api/auth/change-password",
        json={"current_password": "not-it", "new_password": NEW_PASSWORD},
        headers=auth(user),
    )
    assert wrong.status_code == 400
    ok = await client.post(
        "/api/auth/change-password",
        json={"user_id": str(user.id), "current_password": PASSWORD, "new_password": NEW_PASSWORD},
        headers=auth(user),
    )
    assert ok.status_code == 200 and ok.json()["access_token"]
    assert (await login(client, user.email, NEW_PASSWORD)).status_code == 200


# --- refresh / logout -------------------------------------------------------------------------


async def test_refresh_rotates_and_detects_reuse(client, factory):
    user = await factory.user()
    await login(client, user.email)
    first = client.cookies[settings.REFRESH_COOKIE_NAME]

    rotated = await client.post("/api/auth/refresh")
    assert rotated.status_code == 200 and rotated.json()["access_token"]
    second = client.cookies[settings.REFRESH_COOKIE_NAME]
    assert second != first

    # a second tab presenting the just-rotated token within the grace window gets a session
    use_refresh_cookie(client, first)
    tab = await client.post("/api/auth/refresh")
    assert tab.status_code == 200

    # outside the grace window, the same token is a replay: the whole family dies
    async with SessionLocal() as s:
        await s.execute(
            update(RefreshToken)
            .where(RefreshToken.rotated_at.is_not(None))
            .values(rotated_at=datetime.now(UTC) - timedelta(minutes=5))
        )
        await s.commit()
    use_refresh_cookie(client, first)
    replay = await client.post("/api/auth/refresh")
    assert replay.status_code == 401 and error_of(replay) == "refresh_reused"
    use_refresh_cookie(client, second)
    after = await client.post("/api/auth/refresh")
    assert after.status_code == 401
    async with SessionLocal() as s:
        alive = await s.scalar(select(RefreshToken.id).where(RefreshToken.revoked_at.is_(None)).limit(1))
    assert alive is None


async def test_refresh_without_cookie_and_logout(client, factory):
    assert (await client.post("/api/auth/refresh")).status_code == 401
    user = await factory.user()
    await login(client, user.email)
    cookie = client.cookies[settings.REFRESH_COOKIE_NAME]
    out = await client.post("/api/auth/logout")
    assert out.status_code == 200
    use_refresh_cookie(client, cookie)
    again = await client.post("/api/auth/refresh")
    assert again.status_code == 401


async def test_refresh_refused_for_disabled_user(client, factory):
    user = await factory.user()
    await login(client, user.email)
    async with SessionLocal() as s:
        await s.execute(update(User).where(User.id == user.id).values(disabled_at=datetime.now(UTC)))
        await s.commit()
    assert (await client.post("/api/auth/refresh")).status_code == 401


# --- me -----------------------------------------------------------------------------------------


async def test_me_get_and_patch(client, factory):
    user = await factory.user(role="admin", full_name="Boss")
    assert (await client.get("/api/auth/me")).status_code == 401
    bad = await client.get("/api/auth/me", headers={"Authorization": "Bearer nope"})
    assert bad.status_code == 401
    me = await client.get("/api/auth/me", headers=auth(user))
    assert me.status_code == 200
    body = me.json()
    assert body["role"] == "admin" and body["full_name"] == "Boss"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}", body["created_date"])
    patched = await client.patch(
        "/api/auth/me", json={"full_name": "Chief", "language": "fr"}, headers=auth(user)
    )
    assert patched.json()["full_name"] == "Chief" and patched.json()["language"] == "fr"


# --- rate limiting --------------------------------------------------------------------------------


async def test_rate_limit_answers_429_with_the_base44_wording(client):
    responses = [
        await client.post("/api/auth/resend-otp", json={"email": "x@example.test"}) for _ in range(8)
    ]
    limited = responses[-1]
    assert limited.status_code == 429
    assert error_of(limited) == "rate_limited"
    assert "Rate limit exceeded" in limited.json()["message"]


# --- Google ------------------------------------------------------------------------------------


async def test_google_disabled_without_client_id(client):
    # Browser navigations: back to the app's Welcome with the reason, never a JSON page.
    response = await client.get("/api/auth/google/start")
    assert response.status_code == 302
    assert response.headers["location"] == "http://localhost:5190/Welcome?auth_error=google_disabled"
    callback = await client.get("/api/auth/google/callback", params={"code": "x", "state": "y"})
    assert callback.status_code == 302
    assert callback.headers["location"] == "http://localhost:5190/Welcome?auth_error=google_disabled"


async def test_google_disabled_returns_to_the_calling_app_origin(client):
    response = await client.get(
        "/api/auth/google/start", params={"next": "http://127.0.0.1:5190/CustomerHome?x=1"}
    )
    assert response.headers["location"] == "http://127.0.0.1:5190/Welcome?auth_error=google_disabled"
    evil = await client.get("/api/auth/google/start", params={"next": "https://evil.example/"})
    assert evil.headers["location"] == "http://localhost:5190/Welcome?auth_error=google_disabled"


@pytest.fixture
def google_enabled(monkeypatch):
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "client-id.test")
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "client-secret.test")

    profile = {
        "sub": "google-sub-1",
        "email": "g.user@example.test",
        "email_verified": True,
        "name": "G User",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "google-access", "id_token": "x"})
        if request.url.host == "openidconnect.googleapis.com":
            assert request.headers["Authorization"] == "Bearer google-access"
            return httpx.Response(200, json=profile)
        raise AssertionError(f"unexpected call to {request.url}")

    monkeypatch.setattr(
        google_oauth, "http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    return profile


async def _google_round_trip(client, next_url: str) -> httpx.Response:
    start = await client.get("/api/auth/google/start", params={"next": next_url})
    assert start.status_code == 302
    location = urlparse(start.headers["location"])
    assert location.netloc == "accounts.google.com"
    state = parse_qs(location.query)["state"][0]
    return await client.get("/api/auth/google/callback", params={"code": "auth-code", "state": state})


async def test_google_links_existing_account_and_returns_to_next(client, factory, google_enabled):
    existing = await factory.user(email=google_enabled["email"], password=None, verified=False)
    back = await _google_round_trip(client, "http://localhost:5190/CustomerHome?x=1")
    assert back.status_code == 302
    target = urlparse(back.headers["location"])
    assert f"{target.scheme}://{target.netloc}{target.path}" == "http://localhost:5190/CustomerHome"
    query = parse_qs(target.query)
    assert query["x"] == ["1"] and query["access_token"][0]
    assert settings.REFRESH_COOKIE_NAME in back.headers["set-cookie"]
    async with SessionLocal() as s:
        linked = await s.get(User, existing.id)
    assert linked.google_sub == "google-sub-1" and linked.email_verified_at is not None


async def test_google_creates_account_and_refuses_foreign_next(client, google_enabled):
    back = await _google_round_trip(client, "https://evil.example.com/steal")
    assert back.headers["location"].startswith(f"{settings.PUBLIC_APP_URL}/?access_token=")
    async with SessionLocal() as s:
        created = (await s.execute(select(User).where(User.email == google_enabled["email"]))).scalar_one()
    assert created.google_sub == "google-sub-1" and created.password_hash is None


async def test_google_unverified_email_and_bad_state(client, google_enabled):
    google_enabled["email_verified"] = False
    back = await _google_round_trip(client, "/")
    assert "auth_error=google_email_unverified" in back.headers["location"]
    forged = await client.get("/api/auth/google/callback", params={"code": "c", "state": "forged"})
    assert "auth_error=google_state" in forged.headers["location"]
