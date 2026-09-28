"""NoResponseCase: `no_response_cases` in the legacy shape (docs/FIELD_MAPPING.md, NoResponseCase).

Read: admins, and the parties of the order (its customer, its courier, the courier who opened
the case). Base44 kept the entity admin-only because the parties read the copies on Order
(no_response_*); those copies are derived from this table here, so the parties may read the
source rows too (nothing in them is private to the other party).
Write: none. The procedure is triggerEmergencyContact / createHotDeal / cancelOrder / the sweep.
"""

from typing import Any

from sqlalchemy import or_, select, true

from app.compat.registry import EntityDef, LegacyField, register
from app.models import Courier, NoResponseCase, Order, User
from app.security.deps import CurrentUser

cases = NoResponseCase.__table__
case_order = Order.__table__.alias("case_order")
case_customer = User.__table__.alias("case_customer")
case_courier = Courier.__table__.alias("case_courier")
case_courier_user = User.__table__.alias("case_courier_user")
couriers = Courier.__table__


def read_policy(user: CurrentUser) -> Any:
    if user.is_admin:
        return true()
    mine = select(couriers.c.id).where(couriers.c.user_id == user.id)
    return or_(
        case_order.c.customer_id == user.id,
        case_order.c.courier_id.in_(mine),
        cases.c.courier_id.in_(mine),
    )


FIELDS: dict[str, LegacyField] = {
    "order_id": LegacyField(cases.c.order_id, "id"),
    "customer_id": LegacyField(case_customer.c.email, "string"),
    "courier_id": LegacyField(cases.c.courier_id, "id"),
    "courier_user_id": LegacyField(case_courier_user.c.email, "string"),
    "purchase_amount": LegacyField(cases.c.purchase_amount, "number"),
    "started_at": LegacyField(cases.c.started_at, "datetime"),
    "deadline_at": LegacyField(cases.c.deadline_at, "datetime"),
    "status": LegacyField(cases.c.status, "string"),
    "final_at": LegacyField(cases.c.final_at, "datetime"),
    "resolution": LegacyField(cases.c.resolution, "string"),
    "resolved_at": LegacyField(cases.c.resolved_at, "datetime"),
    "incident_counted": LegacyField(cases.c.incident_counted, "boolean"),
    "customer_answered_late": LegacyField(cases.c.customer_answered_late, "boolean"),
    "push_devices": LegacyField(cases.c.channels["push_devices"].as_integer(), "integer"),
    "messaging_status": LegacyField(cases.c.messaging_status, "string"),
}

ENTITY = register(
    EntityDef(
        name="NoResponseCase",
        source=cases.join(case_order, case_order.c.id == cases.c.order_id)
        .join(case_customer, case_customer.c.id == case_order.c.customer_id)
        .outerjoin(case_courier, case_courier.c.id == cases.c.courier_id)
        .outerjoin(case_courier_user, case_courier_user.c.id == case_courier.c.user_id),
        id_expr=cases.c.id,
        id_type="uuid",
        created_expr=cases.c.created_at,
        updated_expr=cases.c.updated_at,
        fields=FIELDS,
        read_policy=read_policy,
    )
)
