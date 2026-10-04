"""OrderOffer: `order_offers` in the legacy shape (docs/FIELD_MAPPING.md, OrderOffer).

Read (base44/entities/OrderOffer.jsonc): the courier who made it, the order's customer,
admins. Create / update: none (createOrderOffer, acceptOrderOffer, cancelOrder). Delete:
the courier's own pending offer = withdrawal (kept as 'withdrawn', which no longer exists
as an entity row, like a Base44 delete); admins too.
"""

import uuid
from typing import Any

from sqlalchemy import Text, and_, cast, exists, func, literal, not_, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.compat.registry import EntityDef, LegacyField, register
from app.errors import ApiError
from app.models import Courier, Order, OrderOffer, User, UserBlock, courier_stats
from app.security.deps import CurrentUser
from app.services.offers import withdraw_offer
from app.services.orders import OrderRefused

offers = OrderOffer.__table__
offer_order = Order.__table__.alias("offer_order")
offer_customer = User.__table__.alias("offer_customer")
offer_courier = Courier.__table__.alias("offer_courier")
offer_courier_user = User.__table__.alias("offer_courier_user")
couriers = Courier.__table__
block_courier = Courier.__table__.alias("block_courier")


def read_policy(user: CurrentUser) -> Any:
    if user.is_admin:
        return true()
    mine = select(couriers.c.id).where(couriers.c.user_id == user.id)
    blocks = UserBlock.__table__
    blocked = exists().where(
        block_courier.c.id == offers.c.courier_id,
        or_(
            and_(blocks.c.blocker_id == block_courier.c.user_id, blocks.c.blocked_id == user.id),
            and_(blocks.c.blocker_id == user.id, blocks.c.blocked_id == block_courier.c.user_id),
        ),
    )
    received = and_(offer_order.c.customer_id == user.id, not_(blocked))
    return or_(offers.c.courier_id.in_(mine), received)


def _customer_or_admin(user: CurrentUser) -> Any:
    return true() if user.is_admin else offer_order.c.customer_id == user.id


def _stat(column: Any) -> Any:
    return (
        select(func.coalesce(column, 0))
        .where(courier_stats.c.courier_id == offers.c.courier_id)
        .scalar_subquery()
    )


def _delivered_to_customer() -> Any:
    """Orders this courier delivered to this customer ("déjà livré 3 fois chez vous")."""
    past = Order.__table__.alias("offer_past_order")
    return (
        select(func.count())
        .where(
            past.c.customer_id == offer_order.c.customer_id,
            past.c.courier_id == offers.c.courier_id,
            past.c.status == "delivered",
        )
        .scalar_subquery()
    )


for_customer = {"read_guard": _customer_or_admin}

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
    # Aurora offer card, for the order's customer (and admins).
    "courier_ratings_count": LegacyField(_stat(courier_stats.c.ratings_count), "integer", **for_customer),
    "courier_deliveries": LegacyField(_stat(courier_stats.c.total_deliveries), "integer", **for_customer),
    "delivered_to_you": LegacyField(_delivered_to_customer(), "integer", **for_customer),
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
