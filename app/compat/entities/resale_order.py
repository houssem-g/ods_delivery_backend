"""ResaleOrder ("Offre chaude"): `hot_deals` in the legacy shape (docs/FIELD_MAPPING.md, ResaleOrder).

Read: every signed-in user reads the deals still listed
(`status = 'available'`), admins read every deal. Field rules (admin only): the courier's phone
and position, the buyer (e-mail, name, phone) and the delivery address; the buyer gets the
courier's phone from reserveHotDeal, the listing gets a rounded distance from listHotDeals.
Write: none (createHotDeal / reserveHotDeal / cancelOrder / the hourly jobs) → 403.
Realtime: a deal leaving the listing (sold, expired, purged) is announced as `delete` to every
subscriber (for all but admins the row is gone), see `app.services.hot_deals.announce`.
"""

from typing import Any

from sqlalchemy import Boolean, and_, false, not_, true

from app.compat.entities.shop import photo_expr
from app.compat.registry import EntityDef, LegacyField, register
from app.models import Courier, HotDeal, Order, User
from app.security.deps import CurrentUser
from app.services.geo import lat_of, lng_of
from app.services.hot_deals import current_price_sql
from app.services.orders import TEST_ORDER_SQL, is_qa_account

deals = HotDeal.__table__
deal_courier = Courier.__table__.alias("deal_courier")
deal_courier_user = User.__table__.alias("deal_courier_user")
deal_buyer = User.__table__.alias("deal_buyer")
buyer_order = Order.__table__.alias("deal_buyer_order")


def _admin(user: CurrentUser) -> Any:
    return true() if user.is_admin else false()


def read_policy(user: CurrentUser) -> Any:
    if user.is_admin:
        return true()
    if is_qa_account(user.email):
        return deals.c.status == "available"
    # deals born from QA orders stay hidden from real customers (B43)
    return and_(
        deals.c.status == "available",
        not_(deals.c.items_text.op("~*", return_type=Boolean)(TEST_ORDER_SQL)),
    )


private = {"read_guard": _admin}

FIELDS: dict[str, LegacyField] = {
    "original_order_id": LegacyField(deals.c.original_order_id, "id"),
    "courier_id": LegacyField(deals.c.courier_id, "id"),
    "courier_name": LegacyField(deal_courier.c.display_name, "string"),
    "courier_phone": LegacyField(deal_courier.c.phone_e164, "string", **private),
    "items_text": LegacyField(deals.c.items_text, "string"),
    "purchase_amount": LegacyField(deals.c.purchase_amount, "number"),
    "discount_percentage": LegacyField(deals.c.discount_percentage, "number"),
    # the decayed price while listed (app/services/hot_deals.current_price), the start price after
    "discounted_price": LegacyField(current_price_sql(deals), "number"),
    "current_price": LegacyField(current_price_sql(deals), "number"),
    "start_price": LegacyField(deals.c.start_price, "number"),
    "floor_price": LegacyField(deals.c.floor_price, "number"),
    "include_delivery": LegacyField(deals.c.include_delivery, "boolean"),
    "delivery_fee": LegacyField(deals.c.delivery_fee, "number"),
    "shop_name": LegacyField(deals.c.shop_name, "string"),
    "shop_address": LegacyField(deals.c.shop_address, "string"),
    "courier_lat": LegacyField(lat_of(deals.c.pickup_location), "number", **private),
    "courier_lng": LegacyField(lng_of(deals.c.pickup_location), "number", **private),
    "photo_url": LegacyField(photo_expr(deals.c.photo_key), "string"),
    "status": LegacyField(deals.c.status, "string"),
    "expires_at": LegacyField(deals.c.expires_at, "datetime"),
    "buyer_id": LegacyField(deal_buyer.c.email, "string", **private),
    "buyer_name": LegacyField(buyer_order.c.contact_name, "string", **private),
    "buyer_phone": LegacyField(buyer_order.c.contact_phone_e164, "string", **private),
    "delivery_address": LegacyField(buyer_order.c.delivery_address, "string", **private),
}

ENTITY = register(
    EntityDef(
        name="ResaleOrder",
        source=deals.join(deal_courier, deal_courier.c.id == deals.c.courier_id)
        .join(deal_courier_user, deal_courier_user.c.id == deal_courier.c.user_id)
        .outerjoin(deal_buyer, deal_buyer.c.id == deals.c.buyer_id)
        .outerjoin(buyer_order, buyer_order.c.id == deals.c.buyer_order_id),
        id_expr=deals.c.id,
        id_type="uuid",
        created_expr=deals.c.created_at,
        updated_expr=deals.c.updated_at,
        created_by_expr=deal_courier_user.c.email,
        fields=FIELDS,
        read_policy=read_policy,
    )
)
