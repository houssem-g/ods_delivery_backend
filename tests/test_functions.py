import pytest

from app.api.functions import FUNCTIONS, RETIRED, FunctionDef
from app.api.functions.getSupportContacts import normalize_support_phone
from app.db import SessionLocal
from app.models import AppSetting
from tests.factories import auth, error_of


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("22 111 222", "+21622111222"),
        ("+216 22 111 222", "+21622111222"),
        ("00216-71-123-456", "+21671123456"),
        ("21698765432", "+21698765432"),
        ("+33 6 12 34 56 78", "+33612345678"),
        ("0041 79 123 45 67", "+41791234567"),
    ],
)
def test_support_phone_accepted(raw, expected):
    assert normalize_support_phone(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "", "   ", None, "123", "0612345678", "+216 12 345 678", "+216 22 111 22", "abc22111222",
        "22111222<script>", "+1 2", "++21622111222", "22+111222", "+2162211122233333333",
        "javascript:alert(1)",
    ],
)  # fmt: skip
def test_support_phone_refused(raw):
    assert normalize_support_phone(raw) is None


async def test_get_support_contacts_is_anonymous_and_revalidates(client, factory):
    empty = await client.post("/api/functions/getSupportContacts")
    assert empty.status_code == 200
    assert empty.json() == {"success": True, "support_phone": None, "support_whatsapp": None}

    admin = await factory.user(role="admin")
    async with SessionLocal() as s:
        s.add(AppSetting(key="main", value={"support_phone": "22 111 222", "support_whatsapp": "not a phone"},
                         updated_by=admin.id))  # fmt: skip
        await s.commit()
    signed_out = await client.post("/api/functions/getSupportContacts", json={})
    assert signed_out.json() == {"success": True, "support_phone": "+21622111222", "support_whatsapp": None}
    signed_in = await client.post(
        "/api/functions/getSupportContacts", headers=auth(admin), content=b"not json"
    )
    assert signed_in.status_code == 200 and "updated_by" not in signed_in.json()


async def test_retired_and_unknown_functions(client):
    assert len(RETIRED) == 16
    gone = await client.post("/api/functions/createOrder", json={})
    assert gone.status_code == 410 and gone.json() == {"error": "gone", "function": "createOrder"}
    missing = await client.post("/api/functions/noSuchFunction", json={})
    assert missing.status_code == 404 and error_of(missing) == "function_not_found"


async def test_invalid_token_is_401_even_for_anonymous_functions(client):
    response = await client.post("/api/functions/getSupportContacts", headers={"Authorization": "Bearer bad"})
    assert response.status_code == 401


@pytest.fixture
def probe_function():
    """A user-only function that writes, to check auth and commit/rollback by status."""
    calls = []

    async def handle(payload, user, session, request):
        calls.append(user.email)
        session.add(AppSetting(key=payload["key"], value={}))
        return payload.get("status", 200), {"ok": True, "who": user.email}

    FUNCTIONS["probeFunction"] = FunctionDef(name="probeFunction", handle=handle, auth="user")
    yield calls
    FUNCTIONS.pop("probeFunction")


async def test_user_function_requires_session_and_commits_only_success(client, factory, probe_function):
    denied = await client.post("/api/functions/probeFunction", json={"key": "a"})
    assert denied.status_code == 401 and denied.json() == {"error": "Unauthorized"}

    user = await factory.user()
    ok = await client.post("/api/functions/probeFunction", json={"key": "kept"}, headers=auth(user))
    assert ok.status_code == 200 and ok.json() == {"ok": True, "who": user.email}
    refused = await client.post(
        "/api/functions/probeFunction", json={"key": "dropped", "status": 409}, headers=auth(user)
    )
    assert refused.status_code == 409
    async with SessionLocal() as s:
        assert await s.get(AppSetting, "kept") is not None
        assert await s.get(AppSetting, "dropped") is None


def test_discovery_found_the_ported_functions():
    assert FUNCTIONS["getSupportContacts"].auth == "optional"
    assert not set(FUNCTIONS) & RETIRED
