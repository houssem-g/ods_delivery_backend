"""Order drafts: saveOrderDraft / listOrderDrafts / deleteOrderDraft, the 24 h expiry, the cap,
the hourly purge and placeOrder deleting the draft it came from."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update

from app.db import SessionLocal
from app.jobs.registry import JOBS
from app.models import OrderDraft
from app.services import order_drafts
from tests.factories import auth
from tests.order_helpers import OrderWorld
from tests.test_order_flow_functions import order_form

FORM = {
    "version": 1,
    "step": 2,
    "shopInfo": {"shop_name": "Monoprix", "shop_lat": 35.82, "shop_lng": 10.6},
    "items": [{"name": "  Pain   complet ", "quantity": 2}, {"name": "Lait", "quantity": 1}],
    "deliveryInfo": {"delivery_address": "Rue 1", "package_size": "moyen"},
}


async def fn(client, user, name, body=None):
    return await client.post(f"/api/functions/{name}", json=body or {}, headers=auth(user))


async def drafts_in_db() -> list[OrderDraft]:
    async with SessionLocal() as s:
        return list((await s.execute(select(OrderDraft).order_by(OrderDraft.created_at))).scalars())


async def age(draft_id: str, hours: float) -> None:
    async with SessionLocal() as s:
        delta = timedelta(hours=hours)
        await s.execute(
            update(OrderDraft)
            .where(OrderDraft.id == draft_id)
            .values(
                updated_at=OrderDraft.updated_at - delta,
                expires_at=OrderDraft.expires_at - delta,
            )
        )
        await s.commit()


@pytest.fixture
async def world(factory):
    return await OrderWorld(factory).setup()


async def test_save_creates_then_updates_the_same_draft(client, world):
    r = await fn(client, world.customer, "saveOrderDraft", {"payload": FORM})
    assert r.status_code == 200, r.text
    draft = r.json()["draft"]
    assert draft["title"] == "Pain complet" and draft["payload"] == FORM
    created = datetime.fromisoformat(draft["updated_date"]).replace(tzinfo=UTC)
    expires = datetime.fromisoformat(draft["expires_at"]).replace(tzinfo=UTC)
    assert expires - created == timedelta(hours=24)
    await age(draft["id"], 3)
    changed = {**FORM, "items": []}
    again = await fn(client, world.customer, "saveOrderDraft", {"id": draft["id"], "payload": changed})
    assert again.json()["draft"]["id"] == draft["id"] and again.json()["draft"]["title"] == "Monoprix"
    [row] = await drafts_in_db()
    assert row.expires_at > datetime.now(UTC) + timedelta(hours=23)  # the clock restarts
    titled = await fn(
        client, world.customer, "saveOrderDraft", {"id": draft["id"], "payload": {}, "title": "Courses"}
    )
    assert titled.json()["draft"]["title"] == "Courses"


@pytest.mark.parametrize(
    ("body", "status", "error"),
    [
        ({}, 400, "invalid_payload"),
        ({"payload": [1, 2]}, 400, "invalid_payload"),
        ({"payload": {"notes": "x" * 21000}}, 413, "payload_too_large"),
    ],
)
async def test_save_validation(client, world, body, status, error):
    r = await fn(client, world.customer, "saveOrderDraft", body)
    assert r.status_code == status and r.json() == {"error": error}


async def test_ownership(client, world, factory):
    other = await factory.user(email="other@example.test")
    mine = (await fn(client, world.customer, "saveOrderDraft", {"payload": FORM})).json()["draft"]
    # another user's id: a new draft for him, mine untouched
    theirs = await fn(client, other, "saveOrderDraft", {"id": mine["id"], "payload": {"items_text": "Eau"}})
    assert theirs.json()["draft"]["id"] != mine["id"]
    assert [d["id"] for d in (await fn(client, other, "listOrderDrafts")).json()["drafts"]] == [
        theirs.json()["draft"]["id"]
    ]
    refused = await fn(client, other, "deleteOrderDraft", {"id": mine["id"]})
    assert refused.status_code == 404 and refused.json() == {"error": "draft_not_found"}
    assert (await fn(client, other, "deleteOrderDraft", {"id": "nope"})).status_code == 404
    ok = await fn(client, world.customer, "deleteOrderDraft", {"id": mine["id"]})
    assert ok.status_code == 200 and ok.json() == {"success": True}
    assert (await fn(client, world.customer, "listOrderDrafts")).json() == {"drafts": []}


async def test_list_is_newest_first_and_hides_expired(client, world):
    ids = []
    for name in ("A", "B", "C"):
        r = await fn(client, world.customer, "saveOrderDraft", {"payload": {"items_text": name}})
        ids.append(r.json()["draft"]["id"])
    await age(ids[0], 25)  # expired
    await age(ids[2], 1)
    drafts = (await fn(client, world.customer, "listOrderDrafts")).json()["drafts"]
    assert [d["title"] for d in drafts] == ["B", "C"]
    # an expired id is never revived: saving it makes a new draft
    revived = await fn(client, world.customer, "saveOrderDraft", {"id": ids[0], "payload": {}})
    assert revived.json()["draft"]["id"] not in ids


async def test_cap_drops_the_oldest(client, world):
    ids = []
    for i in range(order_drafts.MAX_DRAFTS):
        r = await fn(client, world.customer, "saveOrderDraft", {"payload": {"items_text": f"d{i}"}})
        ids.append(r.json()["draft"]["id"])
        await age(ids[-1], 0.01 * (order_drafts.MAX_DRAFTS - i))  # distinct ages, the first oldest
    extra = await fn(client, world.customer, "saveOrderDraft", {"payload": {"items_text": "new"}})
    drafts = (await fn(client, world.customer, "listOrderDrafts")).json()["drafts"]
    assert len(drafts) == order_drafts.MAX_DRAFTS and drafts[0]["id"] == extra.json()["draft"]["id"]
    assert ids[0] not in {d["id"] for d in drafts}


async def test_purge_job_is_idempotent(client, world):
    keep = (await fn(client, world.customer, "saveOrderDraft", {"payload": FORM})).json()["draft"]
    old = (await fn(client, world.customer, "saveOrderDraft", {"payload": FORM})).json()["draft"]
    await age(old["id"], 30)
    assert "purge_order_drafts" in JOBS
    for expected in (1, 0):
        r = await client.post(
            "/api/admin/jobs/purge_order_drafts/run", headers={"x-cron-token": "test-cron-secret"}
        )
        assert r.json()["result"] == {"drafts_deleted": expected}
    assert [str(d.id) for d in await drafts_in_db()] == [keep["id"]]


async def test_place_order_deletes_its_draft_only_when_placed(client, world, factory):
    draft = (await fn(client, world.customer, "saveOrderDraft", {"payload": FORM})).json()["draft"]
    refused = await fn(
        client, world.customer, "placeOrder", {"order": order_form(items_text=" "), "draft_id": draft["id"]}
    )
    assert refused.status_code == 400
    assert len(await drafts_in_db()) == 1  # kept: the order failed
    # someone else's placement never deletes it
    other = await factory.user(email="o@example.test", phone_e164="+21698000111")
    placed_other = await fn(client, other, "placeOrder", {"order": order_form(), "draft_id": draft["id"]})
    assert placed_other.status_code == 200 and len(await drafts_in_db()) == 1
    placed = await fn(client, world.customer, "placeOrder", {"order": order_form(), "draft_id": draft["id"]})
    assert placed.status_code == 200, placed.text
    async with SessionLocal() as s:
        assert (await s.execute(select(func.count()).select_from(OrderDraft))).scalar_one() == 0
