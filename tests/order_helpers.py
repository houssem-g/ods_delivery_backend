"""Order test data (direct database writes) shared by the order test modules."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select, text

from app.db import SessionLocal
from app.models import (
    Courier,
    Notification,
    Order,
    OrderOffer,
    OrderStatusEvent,
    OrderStop,
    OrderTracking,
    PushDelivery,
    User,
)

SOUSSE_SHOP = (35.8256, 10.6084)
SOUSSE_HOME = (35.8300, 10.6200)
TUNIS = (36.8065, 10.1815)


def pt(lat: float, lng: float) -> str:
    return f"SRID=4326;POINT({lng} {lat})"


def now() -> datetime:
    return datetime.now(UTC)


class OrderWorld:
    """A customer, a verified online courier near the Sousse shop, an admin, helpers."""

    def __init__(self, factory: Any) -> None:
        self.factory = factory

    async def setup(self) -> "OrderWorld":
        f = self.factory
        self.admin = await f.user(email="admin@example.test", role="admin", full_name="Admin")
        self.customer = await f.user(
            email="cust@example.test", full_name="Amel Ben Ali", phone_e164="+21622111222", language="fr"
        )
        self.courier_user = await f.user(email="courier@example.test", full_name="Karim", profile=False)
        self.courier = await self.make_courier(self.courier_user, display_name="Karim Trabelsi")
        return self

    async def make_courier(
        self, user: User, *, at: tuple[float, float] | None = SOUSSE_SHOP, **fields: Any
    ) -> Courier:
        defaults: dict[str, Any] = {
            "verification": "verified",
            "is_online": True,
            "last_location": pt(*at) if at else None,
            "last_seen_at": now(),
            "service_governorate": "Sousse",
            "phone_e164": "+21655123456",
        }
        defaults.update(fields)
        return await self.factory.courier(user, **defaults)

    async def order(
        self,
        customer: User | None = None,
        *,
        status: str = "pending",
        courier: Courier | None = None,
        items: str = "2x Pain",
        shop: tuple[float, float] | None = SOUSSE_SHOP,
        delivery: tuple[float, float] | None = SOUSSE_HOME,
        stops: int = 1,
        fee: str | None = None,
        purchase: str | None = None,
        governorate: str | None = "Sousse",
        at_door: bool = False,
        **fields: Any,
    ) -> Order:
        customer = customer or self.customer
        async with SessionLocal() as s:
            order = Order(
                id=uuid.uuid4(),
                customer_id=customer.id,
                courier_id=courier.id if courier else None,
                status=status,
                items_text=items,
                contact_name=customer.full_name,
                contact_phone_e164=customer.phone_e164 or "+21622111222",
                delivery_address="Rue de la plage",
                delivery_details="3e étage",
                delivery_governorate="Sousse",
                delivery_location=pt(*delivery) if delivery else None,
                delivery_fee=Decimal(fee) if fee else None,
                purchase_amount=Decimal(purchase) if purchase else None,
                accepted_at=now() if courier else None,
                delivered_at=now() if status == "delivered" else None,
                cancelled_at=now() if status == "cancelled" else None,
                **fields,
            )
            s.add(order)
            await s.flush()
            for seq in range(stops):
                s.add(
                    OrderStop(
                        order_id=order.id,
                        seq=seq,
                        name="Monoprix" if seq == 0 else f"Shop {seq + 1}",
                        address="Av. Habib Bourguiba",
                        governorate=governorate if seq == 0 else None,
                        location=pt(*shop) if shop else None,
                        status="pending",
                    )
                )
            if (
                at_door
                and courier
                and delivery
                and status in ("purchased", "on_the_way", "client_no_response")
            ):
                # the courier's live position at the door: « Client ne répond pas » needs ≤ 300 m (B34)
                s.add(
                    OrderTracking(
                        order_id=order.id, courier_id=courier.id, location=pt(*delivery), recorded_at=now()
                    )
                )
            s.add(OrderStatusEvent(order_id=order.id, to_status="pending", source="test"))
            if status != "pending":
                s.add(
                    OrderStatusEvent(
                        order_id=order.id, from_status="pending", to_status=status, source="test"
                    )
                )
            await s.commit()
            await s.refresh(order)
            return order

    async def offer(
        self, order: Order, courier: Courier | None = None, fee: str = "5", status: str = "pending"
    ) -> OrderOffer:
        async with SessionLocal() as s:
            row = OrderOffer(
                order_id=order.id,
                courier_id=(courier or self.courier).id,
                proposed_fee=Decimal(fee),
                eta_minutes=20,
                distance_km=Decimal("1.5"),
                status=status,
            )
            s.add(row)
            await s.commit()
            await s.refresh(row)
            return row


async def reload(model: Any, key: Any) -> Any:
    async with SessionLocal() as s:
        return await s.get(model, key)


async def rows(stmt: Any) -> list[Any]:
    async with SessionLocal() as s:
        return list((await s.execute(stmt)).scalars())


async def notifications(user: User | None = None, type_: str | None = None) -> list[Notification]:
    stmt = select(Notification).order_by(Notification.created_at, Notification.id)
    if user is not None:
        stmt = stmt.where(Notification.user_id == user.id)
    if type_ is not None:
        stmt = stmt.where(Notification.type == type_)
    return await rows(stmt)


async def pushes(user: User) -> list[PushDelivery]:
    return await rows(select(PushDelivery).where(PushDelivery.user_id == user.id))


async def age(table: str, row_id: Any, **columns: datetime) -> None:
    """Moves timestamps back (the updated_at trigger is bypassed)."""
    assignments = ", ".join(f"{name} = :{name}" for name in columns)
    async with SessionLocal() as s:
        await s.execute(text("SET LOCAL session_replication_role = replica"))
        await s.execute(text(f"UPDATE {table} SET {assignments} WHERE id = :id"), {"id": row_id, **columns})
        await s.commit()


async def age_events(order_id: uuid.UUID, when: datetime) -> None:
    async with SessionLocal() as s:
        await s.execute(
            text("UPDATE order_status_events SET created_at = :w WHERE order_id = :o"),
            {"w": when, "o": order_id},
        )
        await s.commit()


async def age_order(order: Order, hours: float) -> None:
    """The whole order looks `hours` old (creation, last write, events)."""
    when = now() - timedelta(hours=hours)
    await age("orders", order.id, created_at=when, updated_at=when)
    await age_events(order.id, when)


async def set_live(
    order: Order, courier: Courier, lat: float, lng: float, at: datetime | None = None
) -> None:
    async with SessionLocal() as s:
        await s.merge(
            OrderTracking(
                order_id=order.id, courier_id=courier.id, location=pt(lat, lng), recorded_at=at or now()
            )
        )
        await s.commit()


async def device(user: User, token: str | None = None) -> None:
    from app.models import DeviceToken

    async with SessionLocal() as s:
        s.add(DeviceToken(user_id=user.id, token=token or f"tok-{uuid.uuid4().hex}", platform="android"))
        await s.commit()
