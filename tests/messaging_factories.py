"""Test data for the messaging tests (orders, offers, messages, device tokens)."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest

from app.db import SessionLocal
from app.models import Courier, DeviceToken, Message, Order, OrderOffer, OrderStop, User
from app.services import whatsapp


async def make_order(
    customer: User, courier: Courier | None = None, status: str | None = None, **fields: Any
) -> Order:
    async with SessionLocal() as s:
        order = Order(
            customer_id=customer.id,
            courier_id=courier.id if courier else None,
            status=status or ("accepted" if courier else "pending"),
            items_text=fields.pop("items_text", "2 baguettes"),
            contact_name=fields.pop("contact_name", "Client"),
            delivery_address=fields.pop("delivery_address", "Rue 1"),
            cancelled_at=datetime.now(UTC) if status == "cancelled" else None,
            **fields,
        )
        s.add(order)
        await s.flush()
        s.add(OrderStop(order_id=order.id, seq=0, name="Carrefour"))
        await s.commit()
        await s.refresh(order)
        return order


async def make_offer(order: Order, courier: Courier, status: str = "pending", fee: str = "5.5") -> OrderOffer:
    async with SessionLocal() as s:
        offer = OrderOffer(order_id=order.id, courier_id=courier.id, proposed_fee=Decimal(fee), status=status)
        s.add(offer)
        await s.commit()
        return offer


async def make_message(
    order: Order,
    sender: User,
    role: str,
    recipient: User | None = None,
    body: str = "hello",
    read: bool = False,
    created_at: datetime | None = None,
) -> Message:
    async with SessionLocal() as s:
        row = Message(
            order_id=order.id,
            sender_id=sender.id,
            recipient_id=recipient.id if recipient else None,
            sender_role=role,
            body=body,
            read_at=datetime.now(UTC) if read else None,
        )
        if created_at is not None:
            row.created_at = created_at
        s.add(row)
        await s.commit()
        return row


async def make_device(user: User, token: str | None = None, **fields: Any) -> DeviceToken:
    async with SessionLocal() as s:
        row = DeviceToken(
            user_id=user.id, token=token or f"tok-{uuid.uuid4().hex}", platform="android", **fields
        )
        s.add(row)
        await s.commit()
        return row


@dataclass
class Parties:
    customer: User
    courier_user: User
    courier: Courier
    stranger: User
    admin: User


@pytest.fixture
async def parties(factory) -> Parties:
    customer = await factory.user(email="client@example.test", phone_e164="+21698765432")
    courier_user = await factory.user(email="livreur@example.test", role="courier")
    courier = await factory.courier(courier_user, display_name="Sami", verification="verified")
    stranger = await factory.user(email="stranger@example.test")
    admin = await factory.user(email="boss@example.test", role="admin")
    return Parties(customer, courier_user, courier, stranger, admin)


@pytest.fixture
def emitted(monkeypatch) -> list[dict[str, Any]]:
    """Realtime events emitted by the messaging services. (`pending_events` is not enough:
    SQLAlchemy fires before_commit on a SAVEPOINT release too, which publishes and clears
    the pending list inside the transaction.)"""
    from app.realtime import events
    from app.services import device_tokens, messages, notifications

    seen: list[dict[str, Any]] = []
    original = events.emit

    def record(session, entity, type_, id_, audience=None):
        seen.append({"entity": entity, "type": type_, "id": str(id_)})
        original(session, entity, type_, id_, audience)

    for module in (messages, notifications, device_tokens):
        monkeypatch.setattr(module, "emit", record)
    return seen


class HttpRecorder:
    """httpx.MockTransport standing in for Meta and WinSMS: every call is recorded."""

    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        self.responder: Any = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self.responder is None:
            raise AssertionError(f"unexpected network call: {request.url}")
        return self.responder(request, len(self.calls))


@pytest.fixture
def http(monkeypatch) -> HttpRecorder:
    recorder = HttpRecorder()
    monkeypatch.setattr(
        whatsapp, "http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(recorder.handler))
    )

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(whatsapp, "sleep", no_sleep)
    return recorder


@pytest.fixture
def meta_on(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "WHATSAPP_TOKEN", "t")
    monkeypatch.setattr(settings, "WHATSAPP_PHONE_NUMBER_ID", "123")


@pytest.fixture
def sms_on(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "WINSMS_API_KEY", "k")
    monkeypatch.setattr(settings, "WINSMS_SENDER", "ODS")


def ok_wa(msg_id: str = "wamid.1") -> httpx.Response:
    return httpx.Response(200, json={"messaging_product": "whatsapp", "messages": [{"id": msg_id}]})


def wa_error(code: int, status: int = 400) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": f"err {code}"}})


def sms_ok(ref: str = "r1") -> httpx.Response:
    return httpx.Response(200, json={"code": "ok", "ref": ref})
