"""CourierProfile: `couriers` (+ `courier_stats`) in the legacy shape (docs/FIELD_MAPPING.md).

Read (base44/entities/CourierProfile.jsonc): the owner and admins. `id_photo_uri` (the private
ID document key) is not a field at all: admins get short-lived links from getCourierIdPhotos;
`has_id_photo` only says whether there is one.
Write: admins change `verification_status` (verified_at / verified_by recorded, the courier
notified: account_verified / account_rejected); every other write goes through the
updateMyCourierProfile function (create / delete here: 403).
"""

import uuid
from typing import Any

from sqlalchemy import Text, cast, func, literal, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.compat.payload import coerce_payload
from app.compat.registry import EntityDef, LegacyField, register
from app.errors import ApiError
from app.models import Courier, User, courier_stats
from app.security.deps import CurrentUser
from app.services.couriers import set_verification
from app.services.geo import lat_of, lng_of

couriers = Courier.__table__
owner = User.__table__.alias("courier_owner")
stats = courier_stats


def read_policy(user: CurrentUser) -> Any:
    return true() if user.is_admin else couriers.c.user_id == user.id


def _hhmm(column: Any) -> Any:
    return func.to_char(column, "HH24:MI")


FIELDS: dict[str, LegacyField] = {
    "user_id": LegacyField(owner.c.email, "string"),
    "full_name": LegacyField(couriers.c.display_name, "string"),
    "phone": LegacyField(couriers.c.phone_e164, "string"),
    "cin_passport": LegacyField(couriers.c.id_document_number, "string"),
    "photo_url": LegacyField(cast(literal(None), Text), "string"),
    # Whether an ID photo exists (never the key): the admin screen asks getCourierIdPhotos
    # for the couriers where this is true (it looked for id_photo_uri / photo_url, which
    # never leave the server, and so never showed a photo).
    "has_id_photo": LegacyField(couriers.c.id_document_key.is_not(None), "boolean"),
    "vehicle_type": LegacyField(cast(couriers.c.vehicle, Text), "string"),
    "max_package_size": LegacyField(cast(couriers.c.max_package, Text), "string"),
    "price_per_km": LegacyField(couriers.c.price_per_km, "number"),
    "min_fee": LegacyField(couriers.c.min_fee, "number"),
    "is_online": LegacyField(couriers.c.is_online, "boolean"),
    "current_lat": LegacyField(lat_of(couriers.c.last_location), "number"),
    "current_lng": LegacyField(lng_of(couriers.c.last_location), "number"),
    "notification_radius_km": LegacyField(couriers.c.notification_radius_km, "number"),
    "verification_status": LegacyField(cast(couriers.c.verification, Text), "string"),
    "total_deliveries": LegacyField(func.coalesce(stats.c.total_deliveries, 0), "integer"),
    "total_earnings": LegacyField(func.coalesce(stats.c.gross_fees, 0), "number"),
    "average_rating": LegacyField(func.coalesce(stats.c.average_rating, 5), "number"),
    "late_cancellations": LegacyField(couriers.c.late_cancellations, "integer"),
    "service_governorate": LegacyField(couriers.c.service_governorate, "string"),
    "service_country": LegacyField(couriers.c.service_country, "string"),
    "service_city": LegacyField(couriers.c.service_city, "string"),
    "service_start_time": LegacyField(_hhmm(couriers.c.service_start), "string"),
    "service_end_time": LegacyField(_hhmm(couriers.c.service_end), "string"),
    "referral_code": LegacyField(couriers.c.referral_code, "string"),
}


async def update(session: AsyncSession, actor: CurrentUser, doc_id: str, data: dict[str, Any]) -> None:
    if not actor.is_admin:
        raise ApiError(403, "permission_denied", "Permission denied for update operation on CourierProfile")
    values = coerce_payload(ENTITY, data, {"verification_status"})
    try:
        courier_id = uuid.UUID(doc_id)
    except ValueError:
        raise ApiError(404, "not_found", "CourierProfile not found") from None
    courier = (
        await session.execute(select(Courier).where(Courier.id == courier_id).with_for_update())
    ).scalar_one_or_none()
    if courier is None:
        raise ApiError(404, "not_found", "CourierProfile not found")
    if "verification_status" in values:
        await set_verification(session, actor, courier, values["verification_status"])


ENTITY = register(
    EntityDef(
        name="CourierProfile",
        source=couriers.join(owner, owner.c.id == couriers.c.user_id).outerjoin(
            stats, stats.c.courier_id == couriers.c.id
        ),
        id_expr=couriers.c.id,
        id_type="uuid",
        created_expr=couriers.c.created_at,
        updated_expr=couriers.c.updated_at,
        created_by_expr=owner.c.email,
        fields=FIELDS,
        read_policy=read_policy,
        update=update,
    )
)
