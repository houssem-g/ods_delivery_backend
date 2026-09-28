"""OrderOffer: `order_offers` in the legacy shape (docs/FIELD_MAPPING.md, OrderOffer).

Read (base44/entities/OrderOffer.jsonc): the courier who made it, the order's customer,
admins. Create / update: none (createOrderOffer, acceptOrderOffer, cancelOrder). Delete:
the courier's own pending offer = withdrawal (kept as 'withdrawn', which no longer exists
as an entity row, like a Base44 delete); admins too.
"""

import uuid
from typing import Any

from sqlalchemy import Text, cast, literal, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.compat.registry import EntityDef, LegacyField, register
from app.errors import ApiError
from app.models import Courier, Order, OrderOffer, User
from app.security.deps import CurrentUser
from app.services.offers import withdraw_offer
from app.services.orders import OrderRefused

offers = OrderOffer.__table__
offer_order = Order.__table__.alias("offer_order")
offer_customer = User.__table__.alias("offer_customer")
offer_courier = Courier.__table__.alias("offer_courier")
offer_courier_user = User.__table__.alias("offer_courier_user")
couriers = Courier.__table__


def read_policy(user: CurrentUser) -> Any:
    if user.is_admin:
        return true()
    mine = select(couriers.c.id).where(couriers.c.user_id == user.id)
    return or_(offers.c.courier_id.in_(mine), offer_order.c.customer_id == user.id)


FIELDS: dict[str, LegacyField] = {
    "order_id": LegacyField(offers.c.order_id, "id"),
    "customer_id": LegacyField(offer_customer.c.email, "string"),
    "courier_id": LegacyField(offers.c.courier_id, "id"),
    "courier_user_id": LegacyField(offer_courier_user.c.email, "string"),
    "courier_name": LegacyField(offer_courier.c.display_name, "string"),
    "courier_photo": LegacyField(cast(literal(None), Text), "string"),
    "courier_rating": LegacyField(offers.c.courier_rating_snapshot, "number"),
    "courier_vehicle": LegacyField(cast(offer_courier.c.vehicle, Text), "string"),
    "proposed_fee": LegacyField(offers.c.proposed_fee, "number"),
    "eta_minutes": LegacyField(offers.c.eta_minutes, "integer"),
    "distance_km": LegacyField(offers.c.distance_km, "number"),
    "message": LegacyField(offers.c.message, "string"),
    "status": LegacyField(cast(offers.c.status, Text), "string"),
    "created_via": LegacyField(literal("createOrderOffer"), "string"),
}


async def delete(session: AsyncSession, actor: CurrentUser, doc_id: str) -> list[uuid.UUID]:
    try:
        return await withdraw_offer(session, actor, doc_id)
    except OrderRefused as exc:
        if exc.status == 404:
            raise ApiError(404, "not_found", "OrderOffer not found") from exc
        raise ApiError(
            403, "permission_denied", "Permission denied for delete operation on OrderOffer"
        ) from exc


ENTITY = register(
    EntityDef(
        name="OrderOffer",
        source=offers.join(offer_order, offer_order.c.id == offers.c.order_id)
        .join(offer_customer, offer_customer.c.id == offer_order.c.customer_id)
        .join(offer_courier, offer_courier.c.id == offers.c.courier_id)
        .join(offer_courier_user, offer_courier_user.c.id == offer_courier.c.user_id),
        id_expr=offers.c.id,
        id_type="uuid",
        created_expr=offers.c.created_at,
        updated_expr=offers.c.updated_at,
        created_by_expr=offer_courier_user.c.email,
        fields=FIELDS,
        read_policy=read_policy,
        base_where=offers.c.status != "withdrawn",
        delete=delete,
    )
)
