"""Courier documents: listMyCourierDocuments, uploadCourierDocument, reviewCourierDocument,
listCourierDocuments, and their cleanup at account deletion."""

import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import CourierDocument, File
from app.services import account_deletion
from tests.factories import auth
from tests.order_helpers import OrderWorld, notifications, now, reload, rows


@pytest.fixture
async def world(factory):
    return await OrderWorld(factory).setup()


async def call(client, user, name: str, payload: dict[str, Any] | None = None):
    return await client.post(f"/api/functions/{name}", json=payload or {}, headers=auth(user))


async def private_file(
    owner, *, content_type="image/jpeg", purpose="generic", ext="jpg", visibility="private"
) -> str:
    key = f"{visibility}/{purpose}/{owner.id}/{uuid.uuid4().hex}.{ext}"
    async with SessionLocal() as s:
        s.add(
            File(
                key=key,
                owner_id=owner.id,
                visibility=visibility,
                purpose=purpose,
                content_type=content_type,
                size_bytes=10,
            )
        )
        await s.commit()
    return key


def tomorrow() -> str:
    return (now() + timedelta(days=400)).date().isoformat()


async def test_synthetic_cin_until_one_is_uploaded(client, world):
    r = await call(client, world.courier_user, "listMyCourierDocuments")
    assert r.status_code == 200, r.text
    [cin] = r.json()["documents"]
    assert cin["kind"] == "cin" and cin["synthetic"] is True and cin["id"] is None
    assert cin["status"] == "verified" and cin["has_file"] is False
    assert (await call(client, world.customer, "listMyCourierDocuments")).json()[
        "error"
    ] == "courier_profile_missing"

    key = await private_file(world.courier_user)
    up = await call(client, world.courier_user, "uploadCourierDocument", {"kind": "cin", "file_url": key})
    assert up.status_code == 200, up.text
    docs = (await call(client, world.courier_user, "listMyCourierDocuments")).json()["documents"]
    assert [(d["kind"], d["synthetic"], d["status"]) for d in docs] == [("cin", False, "pending")]
    assert "file_url" not in docs[0] and "file_key" not in docs[0]


async def test_upload_replace_and_review(client, world):
    key = await private_file(world.courier_user, content_type="application/pdf", ext="pdf")
    first = await call(
        client,
        world.courier_user,
        "uploadCourierDocument",
        {"kind": "permis", "file_url": key, "expires_on": tomorrow()},
    )
    assert first.status_code == 200, first.text
    doc = first.json()["document"]
    assert doc["kind"] == "permis" and doc["status"] == "pending" and doc["expires_on"] == tomorrow()
    assert doc["expired"] is False and doc["synthetic"] is False
    assert (await rows(select(File.purpose).where(File.key == key))) == ["courier_doc"]

    rejected = await call(
        client,
        world.admin,
        "reviewCourierDocument",
        {"id": doc["id"], "status": "rejected", "note": " floue "},
    )
    assert rejected.status_code == 200, rejected.text
    assert (
        rejected.json()["document"]["status"] == "rejected" and rejected.json()["document"]["note"] == "floue"
    )
    [note] = await notifications(world.courier_user, "document_rejected")
    assert note.title_fr == "❌ Permis de conduire refusé(e)" and note.body_fr.startswith(
        "Document refusé : floue"
    )
    assert note.data["document_id"] == doc["id"] and note.data["recipient_role"] == "courier"

    # a new upload of the same kind replaces the file and goes back to review
    key2 = await private_file(world.courier_user)
    again = await call(
        client, world.courier_user, "uploadCourierDocument", {"kind": "permis", "file_url": key2}
    )
    assert again.json()["document"]["id"] == doc["id"] and again.json()["document"]["status"] == "pending"
    assert again.json()["document"]["note"] is None and again.json()["document"]["expires_on"] is None
    row = await reload(CourierDocument, uuid.UUID(doc["id"]))
    assert row.file_key == key2 and row.reviewed_by is None and len(await rows(select(CourierDocument))) == 1

    ok = await call(client, world.admin, "reviewCourierDocument", {"id": doc["id"], "status": "verified"})
    assert ok.json()["document"]["status"] == "verified"
    [note] = await notifications(world.courier_user, "document_verified")
    assert note.title_ar == "✅ تم قبول رخصة السياقة"
    row = await reload(CourierDocument, uuid.UUID(doc["id"]))
    assert row.reviewed_by == world.admin.id and row.reviewed_at is not None


async def test_upload_refusals(client, world, factory):
    stranger = await factory.user(email="s@example.test")
    theirs = await private_file(stranger)
    public = await private_file(world.courier_user, visibility="public")
    audio = await private_file(world.courier_user, content_type="audio/webm", purpose="chat", ext="webm")
    id_photo = await private_file(world.courier_user, purpose="courier_id")
    mine = await private_file(world.courier_user)
    past = (now() - timedelta(days=3)).date().isoformat()
    cases = [
        ({"kind": "passport", "file_url": mine}, "invalid_kind"),
        ({"kind": "cin"}, "invalid_file"),
        ({"kind": "cin", "file_url": theirs}, "invalid_file"),
        ({"kind": "cin", "file_url": public}, "invalid_file"),
        ({"kind": "cin", "file_url": audio}, "invalid_file"),
        ({"kind": "cin", "file_url": id_photo}, "invalid_file"),
        ({"kind": "cin", "file_url": "private/x/../y.jpg"}, "invalid_file"),
        ({"kind": "cin", "file_url": mine, "expires_on": "31/12/2030"}, "invalid_expires_on"),
        ({"kind": "cin", "file_url": mine, "expires_on": 2030}, "invalid_expires_on"),
        ({"kind": "cin", "file_url": mine, "expires_on": past}, "document_expired"),
        ({"kind": "cin", "file_url": mine, "expires_on": "2201-01-01"}, "invalid_expires_on"),
    ]
    for payload, error in cases:
        r = await call(client, world.courier_user, "uploadCourierDocument", payload)
        assert r.status_code == 400 and r.json()["error"] == error, payload
    assert (
        await call(client, stranger, "uploadCourierDocument", {"kind": "cin", "file_url": theirs})
    ).status_code == 403
    assert await rows(select(CourierDocument)) == []


async def test_admin_list_and_review_guards(client, world, factory, monkeypatch):
    monkeypatch.setattr(
        "app.services.courier_documents.s3.presign_get",
        lambda key, seconds: f"https://signed/{key}?t={seconds}",
    )
    other_user = await factory.user(email="c2@example.test", profile=False)
    other = await world.make_courier(other_user, display_name="Sami")
    k1 = await private_file(world.courier_user)
    k2 = await private_file(other_user)
    d1 = (
        await call(client, world.courier_user, "uploadCourierDocument", {"kind": "assurance", "file_url": k1})
    ).json()
    await call(client, other_user, "uploadCourierDocument", {"kind": "carte_grise", "file_url": k2})
    await call(
        client, world.admin, "reviewCourierDocument", {"id": d1["document"]["id"], "status": "verified"}
    )

    everything = (await call(client, world.admin, "listCourierDocuments")).json()["documents"]
    assert len(everything) == 2
    pending = (await call(client, world.admin, "listCourierDocuments", {"status": "pending"})).json()[
        "documents"
    ]
    assert [(d["kind"], d["courier_name"]) for d in pending] == [("carte_grise", "Sami")]
    assert pending[0]["file_url"] == f"https://signed/{k2}?t=300" and pending[0]["url_expires_in"] == 300
    his = (await call(client, world.admin, "listCourierDocuments", {"courier_id": str(other.id)})).json()
    assert [d["courier_id"] for d in his["documents"]] == [str(other.id)]
    assert (await call(client, world.admin, "listCourierDocuments", {"courier_id": "x"})).json()[
        "documents"
    ] == []
    assert (await call(client, world.admin, "listCourierDocuments", {"status": "x"})).json()[
        "error"
    ] == "invalid_status"

    for user in (world.courier_user, world.customer):
        assert (await call(client, user, "listCourierDocuments")).status_code == 403
        r = await call(
            client, user, "reviewCourierDocument", {"id": d1["document"]["id"], "status": "rejected"}
        )
        assert r.status_code == 403
    cases = [
        ({"id": d1["document"]["id"], "status": "pending"}, 400, "invalid_status"),
        ({"id": d1["document"]["id"], "status": "rejected", "note": 5}, 400, "invalid_note"),
        ({"id": str(uuid.uuid4()), "status": "verified"}, 404, "document_not_found"),
        ({"id": "nope", "status": "verified"}, 404, "document_not_found"),
    ]
    for payload, status, error in cases:
        r = await call(client, world.admin, "reviewCourierDocument", payload)
        assert (r.status_code, r.json()["error"]) == (status, error), payload


async def test_account_deletion_removes_the_documents(client, world, monkeypatch):
    deleted: list[str] = []

    async def fake_delete(key: str) -> None:
        deleted.append(key)

    monkeypatch.setattr(account_deletion.s3, "delete_object", fake_delete)
    key = await private_file(world.courier_user)
    await call(client, world.courier_user, "uploadCourierDocument", {"kind": "permis", "file_url": key})
    r = await client.post("/api/functions/deleteMyAccount", json={}, headers=auth(world.courier_user))
    assert r.status_code == 200, r.text
    assert await rows(select(CourierDocument)) == []
    assert key in deleted
