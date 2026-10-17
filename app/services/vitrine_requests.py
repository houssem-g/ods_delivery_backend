"""« Mettez votre carte sur ODS Delivery » — a restaurant's request (owner 17/10/2026).

A restaurant asks; ONLY an admin creates the Vitrine (app/services/vitrine.py, createVitrine with
`request_id`). Three ways in:
- `app`: a signed-in user of the app (he follows the state in « Ma carte » and gets a push at the
  decision);
- `public`: the shareable page without an account (`/ma-carte`); the device keeps the follow token;
- `ods`: the ODS backend, for one of its restaurants (« Mettre ma carte sur ODS Delivery », owner
  only on the ODS side): `ods_restaurant_id`, `test` from ODS staging. Self-declared: the admin
  checks by phone before creating anything (the request only fills the creation form).

Abuse: a hidden field (`website`) filled → a silent fake success, nothing stored; per address 5/hour
(limiter) and 10/day (database, salted hash — the address itself is never stored); per account
5/day; per phone 3 per 30 days and ONE open request at a time (a second one answers the same
success, without saying that the number is already known); the ODS path is limited per restaurant
(5/day) and globally (200/hour). Free text is cut to its limits and filtered (text_filter).

Privacy: the requester's name, phone, address, photo and message are read by the admins only; the
requester reads his own request (account, or the follow token answered once at creation, stored as
a sha256), never anybody else's.
"""

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.errors import ApiError
from app.models import File, Partner, Shop, User, Vitrine, VitrineRequest
from app.models.vitrine import REQUEST_KINDS, REQUEST_STATUSES
from app.rate_limit import allow
from app.security.deps import CurrentUser
from app.services import phones, text_filter
from app.services.geo import ORDER_BOUNDS, as_float, within
from app.services.notifications import notify
from app.services.orders import is_qa_account
from app.services.shops import clean_text
from app.storage import s3

HONEYPOT = "website"
IP_RATE = "5/hour"
IP_PER_DAY = 10
ODS_IP_PER_DAY = 300  # the ODS backend calls from one address for all its restaurants
USER_PER_DAY = 5
PHONE_PER_30_DAYS = 3
ODS_PER_RESTAURANT = "5/day"
ODS_GLOBAL = "200/hour"
STATUS_RATE = "60/hour"
NAME_MAX, ADDRESS_MAX, MESSAGE_MAX, REASON_MAX = 100, 300, 500, 300
PHOTO_SECONDS = 3600


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return None


def _admin(user: CurrentUser | None) -> None:
    if user is None or not user.is_admin:
        raise ApiError(403, "forbidden", "Admin only")


def token_hash(token: str) -> str:
    return hashlib.sha256(f"vreq:{token}".encode()).hexdigest()


def client_hash(address: str) -> str:
    return hashlib.sha256(f"{settings.JWT_SECRET}|vreq|{address}".encode()).hexdigest()


def _text(value: Any, limit: int) -> str:
    return text_filter.mask(clean_text(value, limit))[0]


def _phone(raw: Any) -> str:
    try:
        e164 = phones.to_e164(str(raw) if raw is not None else None)
    except phones.InvalidPhone:
        e164 = None
    if not e164 or not phones.is_tunisian(e164):
        raise ApiError(400, "invalid_phone", "A Tunisian phone number")
    return e164


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return n if 0 < n < 2**53 else None


# --- views ------------------------------------------------------------------------------------------


async def _shop_ref(session: AsyncSession, shop_id: uuid.UUID | None) -> dict[str, Any] | None:
    if shop_id is None:
        return None
    shop = await session.get(Shop, shop_id)
    return {"id": str(shop.id), "name": shop.name, "slug": shop.slug} if shop is not None else None


async def mine_json(session: AsyncSession, row: VitrineRequest) -> dict[str, Any]:
    """What the requester reads of HIS request (no admin note, no other request)."""
    return {
        "id": str(row.id),
        "status": row.status,
        "source": row.source,
        "shop_name": row.shop_name,
        "created_at": _iso(row.created_at),
        "decided_at": _iso(row.decided_at),
        "refuse_reason": row.refuse_reason if row.status == "refused" else None,
        "shop": await _shop_ref(session, row.shop_id) if row.status == "accepted" else None,
    }


def _photo_url(key: str | None) -> str | None:
    if not key:
        return None
    try:
        return s3.presign_get(key, PHOTO_SECONDS)
    except Exception:  # storage down: the request is still readable without its photo
        return None


async def admin_json(session: AsyncSession, row: VitrineRequest) -> dict[str, Any]:
    partner = None
    if row.ods_restaurant_id is not None:
        found = (
            await session.execute(
                select(Partner)
                .where(Partner.ods_restaurant_id == row.ods_restaurant_id, Partner.kind == "ods_restaurant")
                .order_by(Partner.revoked_at.is_not(None), Partner.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if found is not None:
            linked = (
                await session.execute(select(Vitrine.shop_id).where(Vitrine.partner_id == found.id))
            ).scalar_one_or_none()
            partner = {
                "id": str(found.id),
                "name": found.name,
                "active": bool(found.active and found.revoked_at is None),
                "vitrine_shop_id": str(linked) if linked else None,
            }
    account = None
    if row.user_id is not None:
        user = await session.get(User, row.user_id)
        if user is not None:
            account = {"email": user.email, "full_name": user.full_name}
    return {
        **(await mine_json(session, row)),
        "phone": row.phone,
        "address": row.address,
        "lat": row.lat,
        "lng": row.lng,
        "kind": row.kind,
        "uses_ods": row.uses_ods,
        "ods_restaurant_id": row.ods_restaurant_id,
        "message": row.message,
        "test": row.test,
        "lang": row.lang,
        "menu_photo_url": _photo_url(row.menu_photo_key),
        "account": account,
        "partner": partner,
        "shop": await _shop_ref(session, row.shop_id),
        "refuse_reason": row.refuse_reason,
    }


# --- submit -----------------------------------------------------------------------------------------


async def _count(session: AsyncSession, *where: Any) -> int:
    return int((await session.execute(select(func.count(VitrineRequest.id)).where(*where))).scalar_one())


def _limited() -> ApiError:
    return ApiError(429, "too_many_requests", "Too many requests, try again later")


async def _photo_key(session: AsyncSession, user: CurrentUser | None, raw: Any) -> str | None:
    """A private upload of the requester himself (purpose « menu »), else 400 invalid_photo."""
    if raw in (None, ""):
        return None
    if user is None or not isinstance(raw, str):
        raise ApiError(400, "invalid_photo", "Sign in to add a photo")
    key = raw.strip()[:300]
    row = (await session.execute(select(File).where(File.key == key))).scalar_one_or_none()
    if (
        row is None
        or row.owner_id != user.id
        or row.visibility != "private"
        or row.purpose != "menu"
        or not (row.content_type or "").startswith("image/")
    ):
        raise ApiError(400, "invalid_photo", "Not your photo")
    return key


async def submit(
    session: AsyncSession, user: CurrentUser | None, payload: dict[str, Any], address: str
) -> dict[str, Any]:
    """submitVitrineRequest (account optional) → { success, request, follow_token? }."""
    if clean_text(payload.get(HONEYPOT), 200):
        # a robot filled the hidden field: the same answer as a success, nothing stored
        return {"success": True, "request": {"status": "pending"}}
    from_ods = payload.get("source") == "ods"
    source = "ods" if from_ods else ("app" if user is not None else "public")
    ods_rid = _positive_int(payload.get("ods_restaurant_id")) if from_ods else None
    if from_ods and ods_rid is None:
        raise ApiError(400, "validation_error", "ods_restaurant_id: a positive integer")

    # the cheap limits first (no database read)
    if from_ods:
        if not allow("vitrine_request_ods", str(ods_rid), ODS_PER_RESTAURANT) or not allow(
            "vitrine_request_ods_all", "all", ODS_GLOBAL
        ):
            raise _limited()
    elif not allow("vitrine_request_ip", address, IP_RATE):
        raise _limited()

    name = _text(payload.get("shop_name") or payload.get("name"), NAME_MAX)
    if len(name) < 2:
        raise ApiError(400, "invalid_name", "The restaurant's name")
    phone = _phone(payload.get("phone"))
    place = _text(payload.get("address"), ADDRESS_MAX)
    if len(place) < 3:
        raise ApiError(400, "invalid_address", "The address or the area")
    lat, lng = as_float(payload.get("lat")), as_float(payload.get("lng"))
    if (lat is None) != (lng is None) or (lat is not None and not within(lat, lng, ORDER_BOUNDS)):
        raise ApiError(400, "invalid_location", "A position in Tunisia")
    kind = payload.get("kind") or "restaurant"
    if kind not in REQUEST_KINDS:
        raise ApiError(400, "invalid_kind", f"kind: {', '.join(REQUEST_KINDS)}")
    uses_ods = payload.get("uses_ods")
    uses_ods = True if from_ods else (uses_ods if isinstance(uses_ods, bool) else None)
    message = _text(payload.get("message"), MESSAGE_MAX) or None
    lang = "ar" if payload.get("lang") == "ar" else "fr"
    photo = await _photo_key(session, user, payload.get("menu_photo_key")) if not from_ods else None
    test = (payload.get("test") is True) if from_ods else (user is not None and is_qa_account(user.email))

    now = _now()
    hashed_client = client_hash(address)
    day_ago = now - timedelta(days=1)
    per_ip = await _count(
        session, VitrineRequest.client_hash == hashed_client, VitrineRequest.created_at >= day_ago
    )
    if per_ip >= (ODS_IP_PER_DAY if from_ods else IP_PER_DAY):
        raise _limited()
    if user is not None and not from_ods:
        mine = await _count(session, VitrineRequest.user_id == user.id, VitrineRequest.created_at >= day_ago)
        if mine >= USER_PER_DAY:
            raise _limited()

    # one open request per phone (and per ODS restaurant): never a second one
    same = [VitrineRequest.phone == phone]
    if ods_rid is not None:
        same.append(VitrineRequest.ods_restaurant_id == ods_rid)
    open_rows = (
        (await session.execute(select(VitrineRequest).where(VitrineRequest.status == "pending", or_(*same))))
        .scalars()
        .all()
    )
    if open_rows:
        return await _already_open(session, user, open_rows[0], ods_rid)
    recent = await _count(
        session, VitrineRequest.phone == phone, VitrineRequest.created_at >= now - timedelta(days=30)
    )
    if recent >= PHONE_PER_30_DAYS:
        raise _limited()

    token = secrets.token_urlsafe(32)
    row = VitrineRequest(
        status="pending",
        source=source,
        user_id=user.id if user is not None and not from_ods else None,
        shop_name=name,
        phone=phone,
        address=place,
        lat=lat,
        lng=lng,
        kind=kind,
        uses_ods=uses_ods,
        ods_restaurant_id=ods_rid,
        menu_photo_key=photo,
        message=message,
        test=test,
        lang=lang,
        follow_token_hash=token_hash(token),
        client_hash=hashed_client,
    )
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        # the same phone at the same instant (double tap): the request that won is the one
        winner = (
            (
                await session.execute(
                    select(VitrineRequest).where(
                        VitrineRequest.status == "pending", VitrineRequest.phone == phone
                    )
                )
            )
            .scalars()
            .first()
        )
        if winner is None:
            raise
        return await _already_open(session, user, winner, ods_rid)
    await session.refresh(row)
    return {"success": True, "request": await mine_json(session, row), "follow_token": token}


async def _already_open(
    session: AsyncSession, user: CurrentUser | None, row: VitrineRequest, ods_rid: int | None
) -> dict[str, Any]:
    """A second request while one is open: the requester's own one when it is his, else the same
    generic success (never « this number is already known »)."""
    if ods_rid is not None:
        if row.ods_restaurant_id is None:
            row.ods_restaurant_id, row.uses_ods = ods_rid, True
            row.updated_at = _now()
        return {"success": True, "already_pending": True, "request": {"status": "pending"}}
    if user is not None and row.user_id == user.id:
        return {"success": True, "already_pending": True, "request": await mine_json(session, row)}
    if user is not None and row.user_id is None and row.source == "public":
        # sent without an account, then signed in with the same number: he follows it from now on
        row.user_id = user.id
        row.updated_at = _now()
        return {"success": True, "already_pending": True, "request": await mine_json(session, row)}
    return {"success": True, "request": {"status": "pending"}}


# --- the requester's reads --------------------------------------------------------------------------


async def list_mine(session: AsyncSession, user: CurrentUser, payload: dict[str, Any]) -> dict[str, Any]:
    rows = (
        (
            await session.execute(
                select(VitrineRequest)
                .where(VitrineRequest.user_id == user.id)
                .order_by(VitrineRequest.created_at.desc())
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    return {"success": True, "requests": [await mine_json(session, r) for r in rows]}


async def status_by_token(session: AsyncSession, payload: dict[str, Any], address: str) -> dict[str, Any]:
    """getVitrineRequestStatus (no account): { token } → { success, request } or 404 not_found."""
    if not allow("vitrine_request_status", address, STATUS_RATE):
        raise ApiError(429, "rate_limited", "Rate limit exceeded (too many requests)")
    token = payload.get("token")
    if not isinstance(token, str) or not 20 <= len(token) <= 100:
        raise ApiError(404, "not_found", "Request not found")
    row = (
        await session.execute(
            select(VitrineRequest).where(VitrineRequest.follow_token_hash == token_hash(token))
        )
    ).scalar_one_or_none()
    if row is None:
        raise ApiError(404, "not_found", "Request not found")
    return {"success": True, "request": await mine_json(session, row)}


# --- admin ------------------------------------------------------------------------------------------


async def list_admin(session: AsyncSession, admin: CurrentUser, payload: dict[str, Any]) -> dict[str, Any]:
    _admin(admin)
    wanted = payload.get("status") or "pending"
    stmt = select(VitrineRequest)
    if wanted != "all":
        if wanted not in REQUEST_STATUSES:
            raise ApiError(400, "validation_error", "status: pending, accepted, refused or all")
        stmt = stmt.where(VitrineRequest.status == wanted)
    order = VitrineRequest.created_at.asc() if wanted == "pending" else VitrineRequest.created_at.desc()
    rows = (await session.execute(stmt.order_by(order).limit(200))).scalars().all()
    counts = dict(
        (
            await session.execute(select(VitrineRequest.status, func.count()).group_by(VitrineRequest.status))
        ).all()
    )
    return {
        "success": True,
        "requests": [await admin_json(session, r) for r in rows],
        "counts": {s: int(counts.get(s, 0)) for s in REQUEST_STATUSES},
    }


async def open_request(session: AsyncSession, raw_id: Any) -> VitrineRequest:
    rid = _uuid(raw_id)
    row = await session.get(VitrineRequest, rid, with_for_update=True) if rid is not None else None
    if row is None:
        raise ApiError(404, "request_not_found", "Request not found")
    if row.status != "pending":
        raise ApiError(409, "request_closed", "This request is already decided", decided=row.status)
    return row


async def _tell(session: AsyncSession, row: VitrineRequest) -> None:
    """The requester with an account learns the decision (in-app + push)."""
    if row.user_id is None:
        return
    name = row.shop_name
    if row.status == "accepted":
        title_fr, title_ar = "✅ Votre demande est acceptée", "✅ تم قبول طلبك"
        body_fr = (
            f"« {name} » : nous préparons votre carte sur ODS Delivery. Nous vous appelons pour la vérifier."
        )
        body_ar = f"«{name}»: نجهّز قائمتك على ODS Delivery. سنتصل بك للتثبت منها."
    else:
        title_fr, title_ar = "Votre demande n'a pas été retenue", "لم يُقبل طلبك"
        body_fr = f"« {name} » — motif : {row.refuse_reason}"
        body_ar = f"«{name}» — السبب: {row.refuse_reason}"
    await notify(
        session,
        user_id=row.user_id,
        type_="vitrine_request",
        title_fr=title_fr,
        title_ar=title_ar,
        body_fr=body_fr,
        body_ar=body_ar,
        metadata={"recipient_role": "customer", "request_id": str(row.id), "status": row.status},
    )


async def mark_accepted(
    session: AsyncSession, admin: CurrentUser, row: VitrineRequest, shop_id: uuid.UUID
) -> None:
    row.status, row.shop_id = "accepted", shop_id
    row.decided_by, row.decided_at, row.updated_at = admin.id, _now(), _now()
    await session.flush()
    await _tell(session, row)


async def accept(session: AsyncSession, admin: CurrentUser, payload: dict[str, Any]) -> dict[str, Any]:
    """acceptVitrineRequest { request_id, shop_id }: the request points to an existing Vitrine (the
    usual path is createVitrine { request_id, … }, which accepts it in the same breath)."""
    _admin(admin)
    row = await open_request(session, payload.get("request_id"))
    sid = _uuid(payload.get("shop_id"))
    if sid is None or await session.get(Vitrine, sid) is None:
        raise ApiError(404, "vitrine_not_found", "Vitrine not found")
    await mark_accepted(session, admin, row, sid)
    return {"success": True, "request": await admin_json(session, row)}


async def refuse(session: AsyncSession, admin: CurrentUser, payload: dict[str, Any]) -> dict[str, Any]:
    """refuseVitrineRequest { request_id, reason (3–300, sent to the requester) }."""
    _admin(admin)
    reason = _text(payload.get("reason"), REASON_MAX)
    if len(reason) < 3:
        raise ApiError(400, "validation_error", "reason: 3 to 300 characters")
    row = await open_request(session, payload.get("request_id"))
    row.status, row.refuse_reason = "refused", reason
    row.decided_by, row.decided_at, row.updated_at = admin.id, _now(), _now()
    await session.flush()
    await _tell(session, row)
    return {"success": True, "request": await admin_json(session, row)}
