"""Courier documents: listMyCourierDocuments, uploadCourierDocument, reviewCourierDocument,
listCourierDocuments, and their cleanup at account deletion."""

import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import AuditLog, CourierDocument, File
from app.services import account_deletion
from app.services import email as email_service
from app.services.courier_reminders import clean_first_name
from app.services.push import push_link
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


async def test_admin_summary_per_courier_and_last_review(client, world, factory, monkeypatch):
    """Owner 21/10: each expected document's state on the admin's courier lists / sheet, never a link;
    the review answer says when the last required one is verified (the admin is then asked)."""
    monkeypatch.setattr(
        "app.services.courier_documents.s3.presign_get",
        lambda key, seconds: f"https://signed/{key}?t={seconds}",
    )
    nour_user = await factory.user(email="nour@example.test", profile=False)
    id_key = await private_file(nour_user, purpose="courier_id")
    nour = await world.make_courier(
        nour_user, display_name="Nour", verification="pending", vehicle="scooter", id_document_key=id_key
    )
    walker_user = await factory.user(email="walk@example.test", profile=False)
    walker = await world.make_courier(
        walker_user, display_name="Walid", verification="pending", vehicle="walking"
    )
    permis = (
        await call(
            client,
            nour_user,
            "uploadCourierDocument",
            {"kind": "permis", "file_url": await private_file(nour_user), "expires_on": tomorrow()},
        )
    ).json()["document"]

    def states(summary):
        return {i["kind"]: (i["status"], i["required"], i["source"]) for i in summary["items"]}

    listed = (
        await call(client, world.admin, "listAdminCouriers", {"verification": "pending", "hide_tests": False})
    ).json()
    by_name = {c["full_name"]: c["documents"] for c in listed["couriers"]}
    assert states(by_name["Nour"]) == {
        "cin": ("pending", True, "onboarding"),
        "permis": ("pending", True, "upload"),
        "carte_grise": ("missing", True, None),
    }
    assert by_name["Nour"]["missing"] == ["carte_grise"] and by_name["Nour"]["ready"] is False
    assert states(by_name["Walid"]) == {"cin": ("missing", True, None)}  # on foot: the ID only
    assert "https://signed" not in str(listed) and id_key not in str(listed)

    sheet = (await call(client, world.admin, "getAdminCourierSheet", {"courier_id": str(nour.id)})).json()
    assert sheet["courier"]["documents"]["to_review"] == ["cin", "permis"]
    many = await call(
        client,
        world.admin,
        "getCourierDocumentSummaries",
        {"courier_ids": [str(nour.id), str(walker.id), "x"]},
    )
    assert many.status_code == 200 and set(many.json()["documents"]) == {str(nour.id), str(walker.id)}
    for user in (nour_user, world.customer):
        r = await call(client, user, "getCourierDocumentSummaries", {"courier_ids": [str(nour.id)]})
        assert r.status_code == 403
        assert "documents" not in (await call(client, user, "listAdminCouriers")).json()

    # the sign-up ID photo is reviewed in place: it becomes his `cin` row (same private file)
    cin = await call(
        client,
        world.admin,
        "reviewCourierDocument",
        {"courier_id": str(nour.id), "kind": "cin", "status": "verified"},
    )
    assert cin.status_code == 200, cin.text
    assert cin.json()["document"]["kind"] == "cin"
    assert await rows(select(CourierDocument.file_key).where(CourierDocument.kind == "cin")) == [id_key]
    assert (
        await call(
            client,
            world.admin,
            "reviewCourierDocument",
            {"courier_id": str(walker.id), "kind": "cin", "status": "verified"},
        )
    ).status_code == 404
    await call(client, world.admin, "reviewCourierDocument", {"id": permis["id"], "status": "verified"})
    grise = (
        await call(
            client,
            nour_user,
            "uploadCourierDocument",
            {"kind": "carte_grise", "file_url": await private_file(nour_user)},
        )
    ).json()["document"]
    last = (
        await call(client, world.admin, "reviewCourierDocument", {"id": grise["id"], "status": "verified"})
    ).json()
    assert last["courier"]["verification_status"] == "pending"
    assert last["courier"]["documents"]["ready"] is True and last["courier"]["id"] == str(nour.id)
    assert "https://signed" not in str(last)


async def test_remind_courier_documents(client, world, factory, monkeypatch):
    """Owner 21/10 « Relancer »: personal text (first name, exact documents, his language), push + in-app
    + e-mail, one reminder per 12 h, admins only, audit log, shown on the admin's lists."""
    assert clean_first_name("  MOHAMED-nour2 Chaffak") == "Mohamed-nour"
    assert clean_first_name("🛵 مؤيد الرقوبي") == "مؤيد" and clean_first_name("😀 123") == ""
    fr_user = await factory.user(email="mohamed@example.test", profile=False, language="fr")
    mohamed = await world.make_courier(fr_user, display_name="MOHAMED Nour Chaffak", verification="pending")
    ar_user = await factory.user(email="mouaied@example.test", profile=False, language="ar")
    mouaied = await world.make_courier(ar_user, display_name="مؤيد الرقوبي", verification="pending")
    email_service.OUTBOX.clear()

    ask = {"courier_id": str(mohamed.id), "kinds": ["carte_grise", "cin"]}
    for user in (world.courier_user, world.customer):
        assert (await call(client, user, "remindCourierDocuments", ask)).status_code == 403
    assert (
        await call(client, world.admin, "remindCourierDocuments", {**ask, "kinds": ["x"]})
    ).status_code == 400
    r = await call(client, world.admin, "remindCourierDocuments", ask)
    assert r.status_code == 200, r.text
    [note] = await notifications(fr_user, "documents_requested")
    assert note.body_fr == (
        "Bonjour Mohamed, pour activer votre compte livreur ODS Delivery,"
        " envoyez votre CIN et votre carte grise. Ça prend 1 min."
    )
    assert note.data["kinds"] == ["cin", "carte_grise"]
    assert push_link(note.type, None, note.data) == "/CourierProfile?docs=cin,carte_grise&as=courier"
    [mail] = [m for m in email_service.OUTBOX if m.to == "mohamed@example.test"]
    assert mail.text == note.body_fr

    again = await call(client, world.admin, "remindCourierDocuments", ask)
    assert again.status_code == 429 and again.json()["error"] == "reminded_recently"
    assert again.json()["last"]["kinds"] == ["cin", "carte_grise"]
    assert len(await notifications(fr_user, "documents_requested")) == 1

    r = await call(
        client,
        world.admin,
        "remindCourierDocuments",
        {"courier_id": str(mouaied.id), "kinds": ["permis"], "note": "  صورة   واضحة "},
    )
    assert r.status_code == 200, r.text
    [ar_note] = await notifications(ar_user, "documents_requested")
    assert ar_note.body_ar.startswith(
        "مرحباً مؤيد، لتفعيل حسابكم كمندوب في ODS Delivery، يُرجى إرسال رخصة السياقة."
    )
    assert ar_note.body_ar.endswith("رسالة الفريق: « صورة واضحة »")
    [ar_mail] = [m for m in email_service.OUTBOX if m.to == "mouaied@example.test"]
    assert ar_mail.text == ar_note.body_ar

    audit = await rows(select(AuditLog).where(AuditLog.action == "documents_requested").order_by(AuditLog.id))
    assert [(a.entity_id, a.actor_user_id, a.after["kinds"]) for a in audit] == [
        (str(mohamed.id), world.admin.id, ["cin", "carte_grise"]),
        (str(mouaied.id), world.admin.id, ["permis"]),
    ]
    listed = (
        await call(client, world.admin, "listAdminCouriers", {"verification": "pending", "hide_tests": False})
    ).json()
    reminder = next(c for c in listed["couriers"] if c["id"] == str(mohamed.id))["documents"]["reminder"]
    assert reminder["kinds"] == ["cin", "carte_grise"] and reminder["at"] and reminder["next_at"]
