"""« Relancer » (owner 21/10): an admin asks a courier for documents that are missing, refused or expired.

remindCourierDocuments { courier_id, kinds: [cin | permis | carte_grise | assurance | photo], note? (≤ 200) }
  - admin only; at most one reminder per courier every 12 h (429 `reminded_recently`, `next_at`);
  - the courier gets a personal notice, in his language, with his first name and the exact documents
    (`documents_requested`: in-app + push; a tap opens « Mes documents »), and the same text by e-mail
    when his account has an address (never blocks: a failed e-mail is logged);
  - one audit_log row (entity Courier, action documents_requested): who, when, what, the note. It is also
    where the admin's courier card reads « Relancé le 21/10 à 21h45 : CIN, carte grise ».
"""

import logging
import re
import uuid
from datetime import datetime, timedelta
from html import escape
from typing import Any

from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog, Courier
from app.models.identity import COURIER_DOCUMENT_KINDS, User
from app.security.deps import CurrentUser
from app.services import order_transitions as ot
from app.services.email_templates import RenderedEmail
from app.services.notifications import notify
from app.services.orders import OrderRefused

log = logging.getLogger("odsd.courier_reminders")

ACTION = "documents_requested"
EVERY = timedelta(hours=12)
NOTE_MAX = 200
# « envoyez votre CIN et votre carte grise » / « يُرجى إرسال بطاقة التعريف والبطاقة الرمادية »
ASKED = {
    "cin": ("votre CIN", "بطاقة التعريف"),
    "permis": ("votre permis de conduire", "رخصة السياقة"),
    "carte_grise": ("votre carte grise", "البطاقة الرمادية"),
    "assurance": ("votre attestation d'assurance", "شهادة التأمين"),
    "photo": ("votre photo de profil", "صورة الملف الشخصي"),
}
_NAME_CHARS = re.compile(r"[^\w'\-]|[\d_]", re.UNICODE)


def clean_first_name(display_name: str | None) -> str:
    """The first word of the name, without digits, emoji or punctuation; « MOHAMED » / « mohamed » →
    « Mohamed » (an Arabic name is kept as written). Empty when nothing is left."""
    for word in (display_name or "").split():
        name = _NAME_CHARS.sub("", word).strip("'-")
        if name:
            if name.isascii() or any(c.isalpha() and c.isascii() for c in name):
                name = name[:1].upper() + name[1:].lower()
            return name[:30]
    return ""


def _join(parts: list[str], last: str) -> str:
    return parts[0] if len(parts) == 1 else f"{', '.join(parts[:-1])}{last}{parts[-1]}"


def texts(first: str, kinds: list[str], verified: bool, note: str | None) -> dict[str, str]:
    fr_list = _join([ASKED[k][0] for k in kinds], " et ")
    ar_items = [ASKED[k][1] for k in kinds]
    ar_list = ar_items[0] if len(ar_items) == 1 else f"{'، '.join(ar_items[:-1])} و{ar_items[-1]}"
    hello_fr = f"Bonjour {first}," if first else "Bonjour,"
    hello_ar = f"مرحباً {first}،" if first else "مرحباً،"
    goal_fr = (
        "garder votre compte livreur ODS Delivery actif"
        if verified
        else "activer votre compte livreur ODS Delivery"
    )
    goal_ar = (
        "للحفاظ على حسابكم كمندوب في ODS Delivery" if verified else "لتفعيل حسابكم كمندوب في ODS Delivery"
    )
    body_fr = f"{hello_fr} pour {goal_fr}, envoyez {fr_list}. Ça prend 1 min."
    body_ar = f"{hello_ar} {goal_ar}، يُرجى إرسال {ar_list}. لن يستغرق ذلك أكثر من دقيقة."
    if note:
        body_fr += f" Message de l'équipe : « {note} »"
        body_ar += f" رسالة الفريق: « {note} »"
    return {
        "title_fr": "📄 Documents à envoyer",
        "title_ar": "📄 وثائق مطلوبة",
        "body_fr": body_fr,
        "body_ar": body_ar,
    }


def _ip(request: Request | None) -> str | None:
    from app.services.super_admin import _ip as ip_of

    return ip_of(request)


async def last_reminders(session: AsyncSession, courier_ids: list[uuid.UUID]) -> dict[str, dict[str, Any]]:
    """courier id (str) → { at, next_at, kinds } of his last reminder (admin lists)."""
    if not courier_ids:
        return {}
    latest = (
        select(AuditLog.entity_id, func.max(AuditLog.id).label("id"))
        .where(
            AuditLog.entity == "Courier",
            AuditLog.action == ACTION,
            AuditLog.entity_id.in_([str(i) for i in courier_ids]),
        )
        .group_by(AuditLog.entity_id)
        .subquery()
    )
    rows = (await session.execute(select(AuditLog).join(latest, AuditLog.id == latest.c.id))).scalars()
    return {r.entity_id: _reminder_view(r) for r in rows}


def _reminder_view(row: AuditLog) -> dict[str, Any]:
    from app.compat.dates import legacy_datetime

    after = row.after or {}
    return {
        "at": legacy_datetime(row.created_at),
        "next_at": legacy_datetime(row.created_at + EVERY),
        "kinds": list(after.get("kinds") or []),
        "email_sent": bool(after.get("email")),
    }


async def remind(
    session: AsyncSession, admin: CurrentUser, payload: dict[str, Any], request: Request | None = None
) -> dict[str, Any]:
    if not admin.is_admin:
        raise OrderRefused(403, "Forbidden")
    try:
        courier_id = uuid.UUID(str(payload.get("courier_id")).strip())
    except (TypeError, ValueError):
        raise OrderRefused(404, "courier_not_found") from None
    raw = payload.get("kinds")
    if not isinstance(raw, list) or not raw or any(k not in COURIER_DOCUMENT_KINDS for k in raw):
        raise OrderRefused(400, "invalid_kinds", kinds=list(COURIER_DOCUMENT_KINDS))
    kinds = [k for k in COURIER_DOCUMENT_KINDS if k in raw]
    note_raw = payload.get("note")
    if note_raw is not None and not isinstance(note_raw, str):
        raise OrderRefused(400, "invalid_note")
    note = " ".join((note_raw or "").split())[:NOTE_MAX] or None
    # the courier's row is locked: two admins tapping together send one reminder
    courier = (
        await session.execute(select(Courier).where(Courier.id == courier_id).with_for_update())
    ).scalar_one_or_none()
    if courier is None:
        raise OrderRefused(404, "courier_not_found")
    now: datetime = ot.now_utc()
    last = (
        await session.execute(
            select(AuditLog)
            .where(
                AuditLog.entity == "Courier", AuditLog.entity_id == str(courier.id), AuditLog.action == ACTION
            )
            .order_by(AuditLog.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if last is not None and last.created_at + EVERY > now:
        raise OrderRefused(429, "reminded_recently", last=_reminder_view(last))
    user = await session.get(User, courier.user_id)
    first = clean_first_name(courier.display_name)
    copy = texts(first, kinds, courier.verification == "verified", note)
    await notify(
        session,
        user_id=courier.user_id,
        type_="documents_requested",
        **copy,
        metadata={"recipient_role": "courier", "kinds": kinds},
    )
    email_to = user.email if user is not None and user.email and user.deleted_at is None else None
    email_sent = False
    if email_to:
        email_sent = await _send_email(email_to, user.language if user else "ar", copy)
    row = AuditLog(
        actor_user_id=admin.id,
        action=ACTION,
        entity="Courier",
        entity_id=str(courier.id),
        before=None,
        after={
            "kinds": kinds,
            "note": note,
            "email": email_sent,
            "language": user.language if user else None,
        },
        ip=_ip(request),
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return {"success": True, "reminder": _reminder_view(row), "texts": copy}


async def _send_email(to: str, language: str, copy: dict[str, str]) -> bool:
    from app.services.email import send_email

    lang = "fr" if language == "fr" else "ar"
    title, body = copy[f"title_{lang}"], copy[f"body_{lang}"]
    direction = "rtl" if lang == "ar" else "ltr"
    rendered = RenderedEmail(
        subject=f"ODS Delivery · {title.lstrip('📄 ').strip()}",
        text=body,
        html=(
            f'<html><body><div dir="{direction}" lang="{lang}" style="font-family:sans-serif">'
            f"<p>{escape(body)}</p></div></body></html>"
        ),
    )
    try:
        await send_email(to, rendered)
    except Exception:  # the notice is written; the e-mail is a bonus
        log.exception("documents reminder e-mail failed")
        return False
    return True
