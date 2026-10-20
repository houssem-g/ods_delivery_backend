"""Super admin (owner 20/10): only contact@ordering-dashboard-solution.com gives or removes the admin
role (setUserAdmin). Other admins, customers and couriers get 403; no self-removal; no removal of a
super admin; the person's sessions are revoked; every change is in audit_log; and no other route
makes anyone admin."""

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import AuditLog, RefreshToken, User
from app.security.tokens import issue_refresh_token
from tests.factories import auth

SUPER = "contact@ordering-dashboard-solution.com"


async def set_admin(client, actor, target, admin=True):
    return await client.post(
        "/api/functions/setUserAdmin", json={"user_id": str(target.id), "admin": admin}, headers=auth(actor)
    )


async def role_of(user) -> str:
    async with SessionLocal() as s:
        return (await s.get(User, user.id)).role


async def live_refresh_tokens(user) -> int:
    async with SessionLocal() as s:
        rows = (
            (
                await s.execute(
                    select(RefreshToken).where(
                        RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None)
                    )
                )
            )
            .scalars()
            .all()
        )
        return len(rows)


async def give_session(user) -> None:
    async with SessionLocal() as s:
        await issue_refresh_token(s, user.id)
        await s.commit()


@pytest.fixture
async def people(factory):
    return {
        # the setting is compared case-insensitively
        "boss": await factory.user(
            email="Contact@Ordering-Dashboard-Solution.com", role="admin", full_name="Boss"
        ),
        "admin": await factory.user(email="qa-admin@example.test", role="admin", full_name="Other admin"),
        "amira": await factory.user(email="amira@gmail.com", full_name="Amira"),
        "sami": await factory.user(email="sami@gmail.com", role="courier", full_name="Sami"),
    }


# --- who may call it ----------------------------------------------------------------------------


async def test_me_says_who_is_super_admin(client, people):
    assert (await client.get("/api/auth/me", headers=auth(people["boss"]))).json()["is_super_admin"] is True
    for name in ("admin", "amira", "sami"):
        assert (await client.get("/api/auth/me", headers=auth(people[name]))).json()[
            "is_super_admin"
        ] is False


async def test_super_admin_grants_then_revokes_and_the_previous_role_comes_back(client, people):
    sami = people["sami"]
    granted = await set_admin(client, people["boss"], sami)
    assert granted.status_code == 200 and granted.json() == {
        "success": True,
        "changed": True,
        "role": "admin",
    }
    assert await role_of(sami) == "admin"
    # his new rights apply at once (the role is read from the database on each request)
    assert (await client.get("/api/auth/me", headers=auth(people["sami"]))).json()["role"] == "admin"
    assert (
        await client.post("/api/functions/listAdminUsers", json={}, headers=auth(sami))
    ).status_code == 200

    revoked = await set_admin(client, people["boss"], sami, admin=False)
    assert revoked.status_code == 200 and revoked.json()["role"] == "courier"
    assert await role_of(sami) == "courier"
    assert (
        await client.post("/api/functions/listAdminUsers", json={}, headers=auth(sami))
    ).status_code == 403

    async with SessionLocal() as s:
        rows = (
            (
                await s.execute(
                    select(AuditLog).where(AuditLog.entity_id == str(sami.id)).order_by(AuditLog.id)
                )
            )
            .scalars()
            .all()
        )
    assert [r.action for r in rows] == ["admin_granted", "admin_revoked"]
    assert all(r.actor_user_id == people["boss"].id and r.created_at is not None for r in rows)
    assert rows[0].before["role"] == "courier" and rows[0].after["role"] == "admin"
    assert rows[1].before["role"] == "admin" and rows[1].after["role"] == "courier"


async def test_removing_an_admin_of_unknown_origin_makes_him_customer(client, people):
    revoked = await set_admin(client, people["boss"], people["admin"], admin=False)
    assert revoked.status_code == 200 and revoked.json()["role"] == "customer"
    assert await role_of(people["admin"]) == "customer"


async def test_unchanged_role_is_a_no_op(client, people):
    again = await set_admin(client, people["boss"], people["admin"])
    assert again.json() == {"success": True, "changed": False, "role": "admin"}
    already = await set_admin(client, people["boss"], people["amira"], admin=False)
    assert already.json()["changed"] is False
    async with SessionLocal() as s:
        assert (await s.execute(select(AuditLog).where(AuditLog.action.like("admin_%")))).first() is None


@pytest.mark.parametrize("caller", ["admin", "amira", "sami"])
async def test_anyone_else_is_refused_even_another_admin(client, people, caller):
    for target, admin in ((people["amira"], True), (people["sami"], True), (people["admin"], False)):
        if target is people[caller]:
            continue
        refused = await set_admin(client, people[caller], target, admin)
        assert refused.status_code == 403
    assert await role_of(people["amira"]) == "customer"
    assert await role_of(people["admin"]) == "admin"


async def test_anonymous_is_refused(client, people):
    response = await client.post(
        "/api/functions/setUserAdmin", json={"user_id": str(people["amira"].id), "admin": True}
    )
    assert response.status_code == 401


async def test_listed_email_without_admin_role_or_disabled_is_not_super_admin(
    client, factory, people, monkeypatch
):
    monkeypatch.setattr(settings, "SUPER_ADMIN_EMAILS", [SUPER, "second@example.test"])
    customer = await factory.user(email="second@example.test")
    assert (await set_admin(client, customer, people["amira"])).status_code == 403
    async with SessionLocal() as s:
        (await s.get(User, people["boss"].id)).disabled_at = datetime.now(UTC)
        await s.commit()
    # a disabled account has no session at all
    assert (await set_admin(client, people["boss"], people["amira"])).status_code == 401


async def test_settings_list_drives_who_is_super_admin(client, people, monkeypatch):
    monkeypatch.setattr(settings, "SUPER_ADMIN_EMAILS", ["QA-ADMIN@example.test"])
    assert (await set_admin(client, people["admin"], people["amira"])).status_code == 200
    assert (await set_admin(client, people["boss"], people["sami"])).status_code == 403


# --- what cannot be done ----------------------------------------------------------------------------


async def test_super_admin_cannot_remove_himself_nor_another_super_admin(
    client, factory, people, monkeypatch
):
    self_removal = await set_admin(client, people["boss"], people["boss"], admin=False)
    assert self_removal.status_code == 409 and self_removal.json()["error"] == "cannot_revoke_self"
    monkeypatch.setattr(settings, "SUPER_ADMIN_EMAILS", [SUPER, "partner@example.test"])
    partner = await factory.user(email="partner@example.test", role="admin")
    other = await set_admin(client, people["boss"], partner, admin=False)
    assert other.status_code == 409 and other.json()["error"] == "cannot_revoke_super_admin"
    assert await role_of(people["boss"]) == "admin" and await role_of(partner) == "admin"


async def test_other_admin_cannot_disable_the_super_admin(client, people):
    response = await client.patch(
        f"/api/entities/UserProfile/{people['boss'].id}",
        json={"is_active": False},
        headers=auth(people["admin"]),
    )
    assert response.status_code == 403
    async with SessionLocal() as s:
        assert (await s.get(User, people["boss"].id)).disabled_at is None


async def test_bad_payloads(client, people):
    boss = people["boss"]
    for body in (
        {},
        {"user_id": "nope", "admin": True},
        {"user_id": str(people["amira"].id), "admin": "yes"},
    ):
        response = await client.post("/api/functions/setUserAdmin", json=body, headers=auth(boss))
        assert response.status_code == 400
    missing = await client.post(
        "/api/functions/setUserAdmin",
        json={"user_id": "00000000-0000-0000-0000-000000000001", "admin": True},
        headers=auth(boss),
    )
    assert missing.status_code == 404


async def test_a_disabled_account_is_not_made_admin(client, factory, people):
    off = await factory.user(email="off@gmail.com", disabled_at=datetime.now(UTC))
    response = await set_admin(client, people["boss"], off)
    assert response.status_code == 409 and await role_of(off) == "customer"


# --- sessions ----------------------------------------------------------------------------------------


async def test_role_change_revokes_the_persons_sessions(client, people):
    amira = people["amira"]
    await give_session(amira)
    await give_session(amira)
    await give_session(people["boss"])
    assert await live_refresh_tokens(amira) == 2
    assert (await set_admin(client, people["boss"], amira)).status_code == 200
    assert await live_refresh_tokens(amira) == 0
    await give_session(amira)
    assert (await set_admin(client, people["boss"], amira, admin=False)).status_code == 200
    assert await live_refresh_tokens(amira) == 0
    assert await live_refresh_tokens(people["boss"]) == 1  # the super admin stays signed in


# --- no other way to become admin -------------------------------------------------------------------


async def test_user_profile_never_gives_admin(client, factory, people):
    amira, admin = people["amira"], people["admin"]
    own = await client.patch(
        f"/api/entities/UserProfile/{amira.id}", json={"role": "admin"}, headers=auth(amira)
    )
    assert own.status_code == 400
    by_admin = await client.patch(
        f"/api/entities/UserProfile/{amira.id}", json={"role": "admin"}, headers=auth(admin)
    )
    assert by_admin.status_code == 400
    fresh = await factory.user(email="fresh@gmail.com", profile=False)
    created = await client.post(
        "/api/entities/UserProfile", json={"user_id": fresh.email, "role": "admin"}, headers=auth(fresh)
    )
    assert created.status_code == 400
    assert await role_of(amira) == "customer" and await role_of(fresh) == "customer"


async def test_me_patch_and_sign_up_never_give_admin(client, people):
    amira = people["amira"]
    await client.patch("/api/auth/me", json={"role": "admin", "is_super_admin": True}, headers=auth(amira))
    assert await role_of(amira) == "customer"
    # signing up with the super admin's address (another account) never makes anyone admin
    response = await client.post(
        "/api/auth/register",
        json={
            "email": "someone.else@example.test",
            "password": "Correct-Horse-9",
            "full_name": "X",
            "role": "admin",
        },
    )
    assert response.status_code == 201
    async with SessionLocal() as s:
        made = (await s.execute(select(User).where(User.email == "someone.else@example.test"))).scalar_one()
        assert made.role == "customer"


def test_only_the_super_admin_service_writes_the_admin_role():
    """Static guard: the only code that sets a role to 'admin' is app/services/super_admin.py
    (messages.SYSTEM is an in-memory CurrentUser, not a database row)."""
    root = Path(__file__).resolve().parents[1] / "app"
    pattern = re.compile(r'role\s*=\s*"admin"')  # code only (ruff writes double quotes)
    found = sorted(
        str(p.relative_to(root))
        for p in root.rglob("*.py")
        if "migrations" not in p.parts and pattern.search(p.read_text())
    )
    assert found == ["services/messages.py", "services/super_admin.py"]
    assignments = sorted(
        str(p.relative_to(root))
        for p in root.rglob("*.py")
        if "migrations" not in p.parts and re.search(r"\.role\s*=[^=]", p.read_text())
    )
    # user_profile.py only accepts customer / courier (and never touches an admin)
    assert assignments == ["compat/entities/user_profile.py", "services/super_admin.py"]


# --- lists and sheets carry the flag ------------------------------------------------------------------


async def test_lists_and_sheets_flag_the_super_admin(client, people):
    users = (
        await client.post(
            "/api/functions/listAdminUsers",
            json={"role": "admin", "hide_tests": False},
            headers=auth(people["admin"]),
        )
    ).json()["users"]
    flags = {u["email"].lower(): u["is_super_admin"] for u in users}
    assert flags == {SUPER: True, "qa-admin@example.test": False}
    sheet = await client.post(
        "/api/functions/getAdminCustomerSheet",
        json={"user_id": str(people["boss"].id)},
        headers=auth(people["admin"]),
    )
    assert (
        sheet.json()["customer"]["is_super_admin"] is True
        and sheet.json()["customer"]["account_role"] == "admin"
    )
