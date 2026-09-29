"""UserProfile: the customer profile, stored on `users` (+ default `user_addresses` row).

Base44 rules (base44/entities/UserProfile.jsonc):
- create: only for oneself (`user_id == user.email`); a second create updates the
  existing profile instead of making a duplicate (Base44 made 3 duplicates);
- read / update: oneself or an admin; delete: admin (the account stays, only the
  customer profile goes away);
- field rules: is_active and is_blacklisted are written by admins only; the
  counters (total_orders, no_response_incidents, last_incident_date) are derived
  and never written; referral attribution is set once, never overwritten.
"""

import uuid
from datetime import datetime
from typing import Any

from geoalchemy2 import Geometry
from sqlalchemy import Boolean, Text, and_, bindparam, case, cast, false, func, select, true
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from app.compat.payload import coerce_payload
from app.compat.registry import EntityDef, LegacyField, register
from app.config import settings
from app.errors import ApiError
from app.models import Courier, User, UserAddress, customer_stats
from app.security.deps import CurrentUser
from app.security.tokens import now_utc, revoke_all_for_user
from app.services.phones import TUNISIA_PREFIX, InvalidPhone, to_e164

users = User.__table__
addr = UserAddress.__table__
stats = customer_stats

# legacy notification_preferences key -> users column
PREFERENCE_COLUMNS = {
    "order_status_changes": "notify_order_status",
    "new_orders": "notify_new_orders",
    "incoming_orders": "notify_incoming_orders",
    "chat_messages": "notify_chat",
    "push_notifications_enabled": "push_enabled",
}
ADDRESS_FIELDS = ("default_address", "country", "default_lat", "default_lng", "governorate", "city")
REFERRAL_FIELDS = ("referred_by_courier_id", "referred_by_code", "referred_at")
SELF_FIELDS = frozenset(
    {
        "phone",
        "role",
        "language",
        "notification_preferences",
        "whatsapp_opt_in",
        "whatsapp_opt_in_at",
        "notify_hot_deals",
    }
    | set(ADDRESS_FIELDS)
    | set(REFERRAL_FIELDS)
)
ADMIN_FIELDS = SELF_FIELDS | {"is_active", "is_blacklisted"}


def _point(coord: str) -> Any:
    fn = func.ST_Y if coord == "lat" else func.ST_X
    return fn(cast(addr.c.location, Geometry))


def _whatsapp_on() -> Any:
    """settings.whatsapp_enabled read at each query (not at import)."""
    return bindparam("whatsapp_on", callable_=lambda: settings.whatsapp_enabled, type_=Boolean, unique=True)


def _read_policy(user: CurrentUser) -> Any:
    return true() if user.is_admin else users.c.id == user.id


FIELDS: dict[str, LegacyField] = {
    "user_id": LegacyField(users.c.email, "string"),
    "phone": LegacyField(users.c.phone_e164, "string"),
    # Tunisian numbers need no verification; a foreign one is verified once confirmed by the
    # WhatsApp code (trg_users_phone_unverify clears the date when the number changes).
    "phone_verified": LegacyField(
        case(
            (users.c.phone_e164.is_(None), false()),
            (users.c.phone_e164.startswith(TUNISIA_PREFIX), true()),
            else_=users.c.phone_verified_at.is_not(None),
        ),
        "boolean",
    ),
    # False while WhatsApp is not configured: the code can't be sent, the number is accepted.
    "phone_verification_required": LegacyField(
        and_(
            _whatsapp_on(),
            users.c.phone_e164.is_not(None),
            ~users.c.phone_e164.startswith(TUNISIA_PREFIX),
            users.c.phone_verified_at.is_(None),
        ),
        "boolean",
    ),
    "phone_verification_available": LegacyField(_whatsapp_on(), "boolean"),
    # An admin's app role is 'admin'; his profile keeps the customer side (as in Base44).
    "role": LegacyField(
        case((users.c.role == "admin", "customer"), else_=cast(users.c.role, Text)), "string"
    ),
    "language": LegacyField(users.c.language, "string"),
    "default_address": LegacyField(func.nullif(addr.c.address, ""), "string"),
    "country": LegacyField(addr.c.country, "string"),
    "default_lat": LegacyField(_point("lat"), "number"),
    "default_lng": LegacyField(_point("lng"), "number"),
    "governorate": LegacyField(addr.c.governorate, "string"),
    "city": LegacyField(addr.c.city, "string"),
    "is_active": LegacyField(users.c.disabled_at.is_(None), "boolean"),
    "total_orders": LegacyField(stats.c.total_orders, "integer"),
    "no_response_incidents": LegacyField(stats.c.no_response_incidents, "integer"),
    "is_blacklisted": LegacyField(users.c.is_blacklisted, "boolean"),
    "last_incident_date": LegacyField(stats.c.last_incident_at, "datetime"),
    "notification_preferences": LegacyField(
        func.jsonb_build_object(
            *[part for key, col in PREFERENCE_COLUMNS.items() for part in (key, users.c[col])], type_=JSONB
        ),
        "object",
    ),
    "referred_by_courier_id": LegacyField(users.c.referred_by_courier_id, "id"),
    "referred_by_code": LegacyField(users.c.referred_by_code, "string"),
    "referred_at": LegacyField(users.c.referred_at, "datetime"),
    "whatsapp_opt_in": LegacyField(users.c.whatsapp_opt_in_at.is_not(None), "boolean"),
    "whatsapp_opt_in_at": LegacyField(users.c.whatsapp_opt_in_at, "datetime"),
    # Aurora opt-in: a new hot deal near the default address (hot_deal_new).
    "notify_hot_deals": LegacyField(users.c.notify_hot_deals, "boolean"),
}


def _bad(message: str) -> ApiError:
    return ApiError(400, "validation_error", message)


async def _default_address(session: AsyncSession, user_id: uuid.UUID) -> UserAddress:
    row = (
        await session.execute(
            select(UserAddress)
            .where(UserAddress.user_id == user_id, UserAddress.is_default)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        row = UserAddress(user_id=user_id, is_default=True, address="", country="TN")
        session.add(row)
        await session.flush()
    return row


async def _apply_address(session: AsyncSession, user: User, values: dict[str, Any]) -> None:
    if not any(name in values for name in ADDRESS_FIELDS):
        return
    row = await _default_address(session, user.id)
    if "default_address" in values:
        row.address = (values["default_address"] or "")[:500]
    if "country" in values:
        country = (values["country"] or "").strip().upper()
        if country and (len(country) != 2 or not country.isalpha()):
            raise _bad("country: expected a 2-letter ISO code")
        row.country = country or None
    for name in ("governorate", "city"):
        if name in values:
            setattr(row, name, values[name][:120] if values[name] else None)
    if "default_lat" in values or "default_lng" in values:
        lat, lng = values.get("default_lat"), values.get("default_lng")
        if lat is None or lng is None:
            if "default_lat" in values and "default_lng" in values:
                row.location = None
                return
            raise _bad("default_lat and default_lng go together")
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise _bad("default_lat / default_lng out of range")
        row.location = f"SRID=4326;POINT({lng} {lat})"


def _apply_preferences(user: User, prefs: dict[str, Any]) -> None:
    for key, value in prefs.items():
        column = PREFERENCE_COLUMNS.get(key)
        if column is None:
            continue
        if not isinstance(value, bool):
            raise _bad(f"notification_preferences.{key}: expected true or false")
        setattr(user, column, value)


async def _apply_referral(session: AsyncSession, user: User, values: dict[str, Any]) -> None:
    if not any(name in values for name in REFERRAL_FIELDS):
        return
    if user.referred_by_courier_id is not None or user.referred_by_code is not None:
        return  # set once at sign-up, never overwritten
    courier_id = values.get("referred_by_courier_id")
    if courier_id is None:
        return
    courier = await session.get(Courier, courier_id)
    if courier is None or courier.user_id == user.id:
        return  # unknown courier or his own link: no attribution, like the front's check
    user.referred_by_courier_id = courier.id
    code = values.get("referred_by_code") or courier.referral_code
    user.referred_by_code = str(code)[:32] if code else None
    referred_at = values.get("referred_at")
    user.referred_at = referred_at if isinstance(referred_at, datetime) else now_utc()


async def _apply(session: AsyncSession, actor: CurrentUser, user: User, values: dict[str, Any]) -> None:
    if "phone" in values:
        try:
            phone = to_e164(values["phone"])
        except InvalidPhone as exc:
            raise _bad("phone: invalid phone number") from exc
        if phone != user.phone_e164:  # a new number is not verified (the same one stays verified)
            user.phone_e164 = phone
            user.phone_verified_at = None
    if "role" in values and user.role != "admin":
        if values["role"] not in ("customer", "courier"):
            raise _bad("role: expected customer or courier")
        user.role = values["role"]
    if "language" in values:
        if values["language"] not in ("ar", "fr"):
            raise _bad("language: expected ar or fr")
        user.language = values["language"]
    if "notification_preferences" in values:
        _apply_preferences(user, values["notification_preferences"])
    if "notify_hot_deals" in values:
        user.notify_hot_deals = bool(values["notify_hot_deals"])
    if "whatsapp_opt_in" in values:
        if values["whatsapp_opt_in"]:
            given = values.get("whatsapp_opt_in_at")
            user.whatsapp_opt_in_at = user.whatsapp_opt_in_at or (given if given else now_utc())
        else:
            user.whatsapp_opt_in_at = None
    await _apply_address(session, user, values)
    await _apply_referral(session, user, values)
    if "is_blacklisted" in values:
        user.is_blacklisted = bool(values["is_blacklisted"])
    if "is_active" in values:
        if not values["is_active"] and user.id == actor.id:
            raise _bad("is_active: an admin cannot disable his own account")
        if values["is_active"]:
            user.disabled_at = None
        elif user.disabled_at is None:
            user.disabled_at = now_utc()
            await revoke_all_for_user(session, user.id)
    await session.flush()


def _parse_id(doc_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(doc_id)
    except ValueError as exc:
        raise ApiError(404, "not_found", "UserProfile not found") from exc


async def _profile_for_write(session: AsyncSession, actor: CurrentUser, doc_id: str) -> User:
    target = _parse_id(doc_id)
    if not actor.is_admin and target != actor.id:
        raise ApiError(404, "not_found", "UserProfile not found")
    user = await session.get(User, target, with_for_update=True)
    if user is None or user.deleted_at is not None or user.profile_created_at is None:
        raise ApiError(404, "not_found", "UserProfile not found")
    return user


async def create(session: AsyncSession, actor: CurrentUser, data: dict[str, Any]) -> str:
    owner = data.get("user_id")
    if not isinstance(owner, str) or owner.strip().lower() != actor.email.lower():
        raise ApiError(403, "permission_denied", "Permission denied for create operation on UserProfile")
    user = await session.get(User, actor.id, with_for_update=True)
    assert user is not None
    values = coerce_payload(ENTITY, data, SELF_FIELDS)
    if user.profile_created_at is None:
        user.profile_created_at = now_utc()
    await _apply(session, actor, user, values)
    return str(user.id)


async def update(session: AsyncSession, actor: CurrentUser, doc_id: str, data: dict[str, Any]) -> None:
    user = await _profile_for_write(session, actor, doc_id)
    allowed = ADMIN_FIELDS if actor.is_admin else SELF_FIELDS
    await _apply(session, actor, user, coerce_payload(ENTITY, data, allowed))


async def delete(session: AsyncSession, actor: CurrentUser, doc_id: str) -> list[uuid.UUID]:
    if not actor.is_admin:
        raise ApiError(403, "permission_denied", "Permission denied for delete operation on UserProfile")
    user = await _profile_for_write(session, actor, doc_id)
    user.profile_created_at = None
    await session.flush()
    return [user.id, actor.id]


ENTITY = register(
    EntityDef(
        name="UserProfile",
        source=users.outerjoin(addr, and_(addr.c.user_id == users.c.id, addr.c.is_default)).outerjoin(
            stats, stats.c.user_id == users.c.id
        ),
        id_expr=users.c.id,
        id_type="uuid",
        created_expr=users.c.profile_created_at,
        updated_expr=func.greatest(users.c.updated_at, addr.c.updated_at),
        created_by_expr=users.c.email,
        fields=FIELDS,
        read_policy=_read_policy,
        base_where=and_(users.c.profile_created_at.is_not(None), users.c.deleted_at.is_(None)),
        create=create,
        update=update,
        delete=delete,
    )
)
