"""Onboarding drafts (owner 18/10/2026, « tout l'onboarding recommence de zéro »):
getOnboardingDraft / saveOnboardingDraft / deleteOnboardingDraft, one draft per (user, kind),
secrets never stored, the 30-day expiry and purge, the courier profile creation and the account
deletion dropping it — and the e-mail code flow the sign-up resumes on (verify-otp signs in, the
code asked for stays valid while the person reads it)."""

import re
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from app.db import SessionLocal
from app.models import OnboardingDraft
from app.services import account_deletion
from app.services import email as email_service
from tests.factories import PASSWORD, auth
from tests.test_auth import last_code
from tests.test_courier_functions import ONBOARDING, private_upload

COURIER_FORM = {
    "v": 1,
    "step": 2,
    "formData": {**ONBOARDING, "photo": "private/generic/x/id.jpg"},
    "activity": {"weekly_deliveries": "10"},
}


async def fn(client, user, name, body=None):
    return await client.post(f"/api/functions/{name}", json=body or {}, headers=auth(user))


async def drafts_in_db() -> list[OnboardingDraft]:
    async with SessionLocal() as s:
        return list((await s.execute(select(OnboardingDraft).order_by(OnboardingDraft.kind))).scalars())


async def test_save_then_get_then_replace_then_delete(client, factory):
    user = await factory.user(profile=False)
    empty = await fn(client, user, "getOnboardingDraft", {"kind": "courier"})
    assert empty.status_code == 200 and empty.json() == {"draft": None}

    saved = await fn(client, user, "saveOnboardingDraft", {"kind": "courier", "payload": COURIER_FORM})
    assert saved.status_code == 200, saved.text
    draft = saved.json()["draft"]
    assert draft["kind"] == "courier" and draft["payload"] == COURIER_FORM
    updated = datetime.fromisoformat(draft["updated_date"]).replace(tzinfo=UTC)
    expires = datetime.fromisoformat(draft["expires_at"]).replace(tzinfo=UTC)
    assert expires - updated == timedelta(days=30)

    got = await fn(client, user, "getOnboardingDraft", {"kind": "courier"})
    assert got.json()["draft"]["payload"] == COURIER_FORM

    later = {**COURIER_FORM, "step": 3}
    await fn(client, user, "saveOnboardingDraft", {"kind": "courier", "payload": later})
    await fn(client, user, "saveOnboardingDraft", {"kind": "customer", "payload": {"city": "Sousse"}})
    rows = await drafts_in_db()
    assert [(r.kind, r.payload.get("step")) for r in rows] == [("courier", 3), ("customer", None)]

    gone = await fn(client, user, "deleteOnboardingDraft", {"kind": "courier"})
    assert gone.json() == {"success": True, "deleted": True}
    again = await fn(client, user, "deleteOnboardingDraft", {"kind": "courier"})
    assert again.json() == {"success": True, "deleted": False}
    assert [r.kind for r in await drafts_in_db()] == ["customer"]


async def test_a_draft_is_only_its_owners(client, factory):
    owner = await factory.user(profile=False)
    other = await factory.user(profile=False)
    await fn(client, owner, "saveOnboardingDraft", {"kind": "courier", "payload": COURIER_FORM})
    assert (await fn(client, other, "getOnboardingDraft", {"kind": "courier"})).json() == {"draft": None}
    await fn(client, other, "deleteOnboardingDraft", {"kind": "courier"})
    assert len(await drafts_in_db()) == 1
    anonymous = await client.post("/api/functions/getOnboardingDraft", json={"kind": "courier"})
    assert anonymous.status_code == 401


async def test_passwords_tokens_and_codes_are_never_stored(client, factory):
    user = await factory.user(profile=False)
    payload = {
        "email": "a@example.test",
        "password": "hunter22",
        "formData": {
            "cin_passport": "08123456",
            "newPassword": "x",
            "access_token": "t",
            "otpCode": "123456",
        },
        "list": [{"client_secret": "s", "keep": 1}],
    }
    r = await fn(client, user, "saveOnboardingDraft", {"kind": "customer", "payload": payload})
    assert r.status_code == 200
    stored = (await drafts_in_db())[0].payload
    assert stored == {
        "email": "a@example.test",
        "formData": {"cin_passport": "08123456"},
        "list": [{"keep": 1}],
    }
    assert not re.search(r"hunter22|123456\"|access_token", str(stored))


@pytest.mark.parametrize(
    ("body", "status", "error"),
    [
        ({"payload": {}}, 400, "invalid_kind"),
        ({"kind": "admin", "payload": {}}, 400, "invalid_kind"),
        ({"kind": "courier"}, 400, "invalid_payload"),
        ({"kind": "courier", "payload": [1]}, 400, "invalid_payload"),
        ({"kind": "courier", "payload": {"notes": "x" * 17000}}, 413, "payload_too_large"),
    ],
)
async def test_invalid_saves_are_refused(client, factory, body, status, error):
    user = await factory.user(profile=False)
    r = await fn(client, user, "saveOnboardingDraft", body)
    assert r.status_code == status and r.json() == {"error": error}
    assert await drafts_in_db() == []


async def test_expired_drafts_are_hidden_then_purged(client, factory):
    user = await factory.user(profile=False)
    await fn(client, user, "saveOnboardingDraft", {"kind": "courier", "payload": COURIER_FORM})
    async with SessionLocal() as s:
        await s.execute(
            update(OnboardingDraft).values(
                updated_at=OnboardingDraft.updated_at - timedelta(days=31),
                expires_at=OnboardingDraft.expires_at - timedelta(days=31),
            )
        )
        await s.commit()
    assert (await fn(client, user, "getOnboardingDraft", {"kind": "courier"})).json() == {"draft": None}
    r = await client.post(
        "/api/admin/jobs/purge_order_drafts/run", headers={"x-cron-token": "test-cron-secret"}
    )
    assert r.json()["result"] == {"drafts_deleted": 0, "onboarding_drafts_deleted": 1}
    assert await drafts_in_db() == []


async def test_creating_the_courier_profile_drops_the_courier_draft(client, factory):
    user = await factory.user(profile=False)
    await fn(client, user, "saveOnboardingDraft", {"kind": "courier", "payload": COURIER_FORM})
    await fn(client, user, "saveOnboardingDraft", {"kind": "customer", "payload": {"city": "Sousse"}})
    key = await private_upload(user)
    r = await fn(
        client,
        user,
        "updateMyCourierProfile",
        {"action": "create", "fields": {**ONBOARDING, "id_photo_uri": key}},
    )
    assert r.status_code == 200, r.text
    assert [d.kind for d in await drafts_in_db()] == ["customer"]


async def test_the_photo_uploaded_during_onboarding_can_be_shown_again(client, factory):
    """The draft keeps the private file URI; after a reload the page asks a signed URL for it."""
    user = await factory.user(profile=False)
    key = await private_upload(user)
    r = await client.post("/api/files/signed-url", json={"file_uri": key}, headers=auth(user))
    assert r.status_code == 200 and r.json()["signed_url"]


async def test_account_deletion_drops_the_drafts(client, factory, monkeypatch):
    async def no_bucket(key: str) -> None:
        return None

    monkeypatch.setattr(account_deletion.s3, "delete_object", no_bucket)
    user = await factory.user()
    await fn(client, user, "saveOnboardingDraft", {"kind": "customer", "payload": {"city": "Sousse"}})
    r = await client.post("/api/functions/deleteMyAccount", headers=auth(user))
    assert r.status_code == 200, r.text
    assert await drafts_in_db() == []


# --- the e-mail code the sign-up resumes on --------------------------------------------------------


def mails_to(to: str) -> int:
    return len([m for m in email_service.OUTBOX if m.to == to])


async def test_verify_otp_signs_in_without_the_password(client):
    """After a reload the app has the e-mail but never the password: the code alone signs in."""
    email = "resume.signup@example.test"
    r = await client.post(
        "/api/auth/register", json={"email": email, "password": PASSWORD, "full_name": "Resume"}
    )
    assert r.status_code == 201
    verified = await client.post("/api/auth/verify-otp", json={"email": email, "otp_code": last_code(email)})
    assert verified.status_code == 200
    body = verified.json()
    assert body["access_token"] and body["user"]["email"] == email and body["user"]["id"]
    assert body["user"]["role"] == "user"
    me = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.status_code == 200


async def test_the_code_stays_valid_when_the_sign_up_is_sent_again_right_away(client):
    """A second tap on « Créer mon compte » (or the same sign-up resent) within the resend delay
    never voids the code already on its way: the e-mail the person opens still works."""
    email = "twice@example.test"
    payload = {"email": email, "password": PASSWORD, "full_name": "Twice"}
    await client.post("/api/auth/register", json=payload)
    sent = mails_to(email)
    code = last_code(email)
    again = await client.post("/api/auth/register", json=payload)
    assert again.status_code == 201
    assert mails_to(email) == sent  # no second e-mail
    login = await client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert login.status_code == 403  # the login refused as unverified never sends a code either
    assert mails_to(email) == sent
    verified = await client.post("/api/auth/verify-otp", json={"email": email, "otp_code": code})
    assert verified.status_code == 200
