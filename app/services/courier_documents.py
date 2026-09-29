"""Courier documents (Aurora "Mes documents"): CIN, permis, carte grise, assurance, photo.

  listMyCourierDocuments   the courier's own rows, plus a synthetic `cin` row derived from his
                           verification when he never uploaded one (onboarding took the ID photo
                           then). No file link: like the ID photo, documents are signed only for
                           admins. (No `photo` row is derived: couriers have no profile photo here.)
  uploadCourierDocument    one of his own private uploads (image or PDF) for a kind; a new upload
                           replaces the file and sends the document back to review (pending).
  reviewCourierDocument    admin: verified / rejected (+ note); the courier is told
                           (document_verified / document_rejected).
  listCourierDocuments     admin: filtered by courier / status, with 5-minute signed links.
The uploaded file's purpose becomes `courier_doc`: /api/files/signed-url refuses it (admins only).
"""

import uuid
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.compat.dates import legacy_datetime
from app.models import Courier, CourierDocument, File
from app.models.identity import COURIER_DOCUMENT_KINDS
from app.security.deps import CurrentUser
from app.services import order_transitions as ot
from app.services.couriers import tunis_day
from app.services.notifications import notify
from app.services.orders import OrderRefused, courier_of_user
from app.storage import keys, s3

SIGNED_SECONDS = 300
NOTE_MAX = 500
ADMIN_LIST_MAX = 200
DOCUMENT_TYPES = {**keys.IMAGE_TYPES, **keys.PRIVATE_EXTRA_TYPES}
LABELS = {
    "cin": ("CIN", "بطاقة التعريف"),
    "permis": ("Permis de conduire", "رخصة السياقة"),
    "carte_grise": ("Carte grise", "البطاقة الرمادية"),
    "assurance": ("Assurance", "التأمين"),
    "photo": ("Photo", "الصورة"),
}


def _uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value).strip())
    except (TypeError, ValueError):
        return None


def view(doc: CourierDocument, today: date | None = None) -> dict[str, Any]:
    today = today or tunis_day(ot.now_utc())
    return {
        "id": str(doc.id),
        "courier_id": str(doc.courier_id),
        "kind": doc.kind,
        "status": doc.status,
        "expires_on": doc.expires_on.isoformat() if doc.expires_on else None,
        "expired": doc.expires_on is not None and doc.expires_on < today,
        "note": doc.note,
        "reviewed_at": legacy_datetime(doc.reviewed_at) if doc.reviewed_at else None,
        "created_date": legacy_datetime(doc.created_at) if doc.created_at else None,
        "updated_date": legacy_datetime(doc.updated_at) if doc.updated_at else None,
        "synthetic": False,
    }


def _synthetic_cin(courier: Courier) -> dict[str, Any]:
    return {
        "id": None,
        "courier_id": str(courier.id),
        "kind": "cin",
        "status": courier.verification,
        "expires_on": None,
        "expired": False,
        "note": courier.rejection_reason if courier.verification == "rejected" else None,
        "reviewed_at": legacy_datetime(courier.verified_at) if courier.verified_at else None,
        "created_date": legacy_datetime(courier.created_at) if courier.created_at else None,
        "updated_date": None,
        "synthetic": True,
        "has_file": courier.id_document_key is not None,
    }


async def _courier(session: AsyncSession, user: CurrentUser) -> Courier:
    courier = await courier_of_user(session, user.id)
    if courier is None:
        raise OrderRefused(403, "courier_profile_missing")
    return courier


async def list_mine(session: AsyncSession, user: CurrentUser) -> dict[str, Any]:
    courier = await _courier(session, user)
    rows = list(
        (
            await session.execute(select(CourierDocument).where(CourierDocument.courier_id == courier.id))
        ).scalars()
    )
    docs = [view(r) for r in rows]
    if not any(r.kind == "cin" for r in rows):
        docs.append(_synthetic_cin(courier))
    docs.sort(key=lambda d: COURIER_DOCUMENT_KINDS.index(d["kind"]))
    return {"success": True, "documents": docs}


def _expires_on(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise OrderRefused(400, "invalid_expires_on")
    try:
        parsed = date.fromisoformat(value.strip()[:10])
    except ValueError:
        raise OrderRefused(400, "invalid_expires_on") from None
    if parsed < tunis_day(ot.now_utc()):
        raise OrderRefused(400, "document_expired")
    if parsed.year > 2100:
        raise OrderRefused(400, "invalid_expires_on")
    return parsed


async def upload(session: AsyncSession, user: CurrentUser, payload: dict[str, Any]) -> dict[str, Any]:
    courier = await _courier(session, user)
    kind = payload.get("kind")
    if kind not in COURIER_DOCUMENT_KINDS:
        raise OrderRefused(400, "invalid_kind", kinds=list(COURIER_DOCUMENT_KINDS))
    raw = payload.get("file_url") or payload.get("file_uri")
    if not isinstance(raw, str) or len(raw) > 512 or not raw.startswith("private/"):
        raise OrderRefused(400, "invalid_file")
    file = (await session.execute(select(File).where(File.key == raw).with_for_update())).scalar_one_or_none()
    if (
        file is None
        or file.owner_id != user.id
        or file.visibility != "private"
        or file.content_type not in DOCUMENT_TYPES
        or file.purpose == "courier_id"  # the onboarding ID photo stays where it is
    ):
        raise OrderRefused(400, "invalid_file")
    expires_on = _expires_on(payload.get("expires_on"))
    doc = (
        await session.execute(
            select(CourierDocument)
            .where(CourierDocument.courier_id == courier.id, CourierDocument.kind == kind)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if doc is None:
        doc = CourierDocument(courier_id=courier.id, kind=kind, file_key=file.key)
        session.add(doc)
    doc.file_key, doc.expires_on, doc.status = file.key, expires_on, "pending"
    doc.note, doc.reviewed_by, doc.reviewed_at = None, None, None
    file.purpose = "courier_doc"  # signed for admins only from now on
    await session.flush()
    await session.refresh(doc)
    return {"success": True, "document": view(doc)}


async def review(session: AsyncSession, admin: CurrentUser, payload: dict[str, Any]) -> dict[str, Any]:
    if not admin.is_admin:
        raise OrderRefused(403, "Forbidden")
    doc_id = _uuid(payload.get("id"))
    status = payload.get("status")
    if status not in ("verified", "rejected"):
        raise OrderRefused(400, "invalid_status")
    note_raw = payload.get("note")
    if note_raw is not None and not isinstance(note_raw, str):
        raise OrderRefused(400, "invalid_note")
    note = " ".join(note_raw.split())[:NOTE_MAX] if note_raw else None
    doc = (
        (
            await session.execute(
                select(CourierDocument).where(CourierDocument.id == doc_id).with_for_update()
            )
        ).scalar_one_or_none()
        if doc_id
        else None
    )
    if doc is None:
        raise OrderRefused(404, "document_not_found")
    doc.status, doc.note = status, note
    doc.reviewed_by, doc.reviewed_at = admin.id, ot.now_utc()
    await session.flush()
    await session.refresh(doc)
    courier = await session.get(Courier, doc.courier_id)
    if courier is not None:
        label_fr, label_ar = LABELS[doc.kind]
        verified = status == "verified"
        reason_fr = f" : {note}" if note and not verified else ""
        reason_ar = f": {note}" if note and not verified else ""
        await notify(
            session,
            user_id=courier.user_id,
            type_="document_verified" if verified else "document_rejected",
            title_fr=f"✅ {label_fr} validé(e)" if verified else f"❌ {label_fr} refusé(e)",
            title_ar=f"✅ تم قبول {label_ar}" if verified else f"❌ تم رفض {label_ar}",
            body_fr="Votre document est validé."
            if verified
            else f"Document refusé{reason_fr}. Envoyez-en un nouveau.",
            body_ar="تم قبول وثيقتك." if verified else f"تم رفض الوثيقة{reason_ar}. أرسل وثيقة جديدة.",
            metadata={
                "recipient_role": "courier",
                "document_id": str(doc.id),
                "kind": doc.kind,
                "status": status,
                "note": note,
            },
        )
    return {"success": True, "document": view(doc)}


async def list_all(session: AsyncSession, admin: CurrentUser, payload: dict[str, Any]) -> dict[str, Any]:
    if not admin.is_admin:
        raise OrderRefused(403, "Forbidden")
    stmt = (
        select(CourierDocument, Courier.display_name)
        .join(Courier, Courier.id == CourierDocument.courier_id)
        .order_by(CourierDocument.updated_at.desc(), CourierDocument.id)
        .limit(ADMIN_LIST_MAX)
    )
    if payload.get("courier_id") not in (None, ""):
        courier_id = _uuid(payload.get("courier_id"))
        if courier_id is None:
            return {"success": True, "documents": []}
        stmt = stmt.where(CourierDocument.courier_id == courier_id)
    if payload.get("status") not in (None, ""):
        if payload.get("status") not in ("pending", "verified", "rejected"):
            raise OrderRefused(400, "invalid_status")
        stmt = stmt.where(CourierDocument.status == payload["status"])
    today = tunis_day(ot.now_utc())
    docs = []
    for doc, name in (await session.execute(stmt)).all():
        docs.append(
            {
                **view(doc, today),
                "courier_name": name,
                "file_url": s3.presign_get(doc.file_key, SIGNED_SECONDS),
                "url_expires_in": SIGNED_SECONDS,
            }
        )
    return {"success": True, "documents": docs}
