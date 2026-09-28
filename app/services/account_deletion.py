"""deleteMyAccount: erase the caller's personal data (Google Play account deletion, GDPR).

Base44 deleted the customer's orders and profiles. Here (audit §6.2) the account is
**anonymized** instead: orders are accounting records and every foreign key to users /
couriers is RESTRICT.

- Refused (409 order_in_progress + ids) while an order is not terminal, as customer or
  as courier (client_no_response included: the courier is at the door with goods).
- users: e-mail replaced by a unique placeholder, name/phone/password/Google link
  cleared, deleted_at + disabled_at set, customer profile gone; addresses and pending
  e-mail codes deleted; every session revoked.
- couriers (kept for the delivered orders): name 'Livreur supprimé', phone / ID number /
  ID photo / position / referral code cleared, offline, verification 'rejected'; his
  non-accepted offers deleted, available hot deals expired, live positions deleted.
- orders placed: kept, contact snapshot anonymized (name, phone, address, details, notes,
  position); orders delivered: kept (the courier name comes from the anonymized row).
- messages sent: text replaced by '[message supprimé]'; notifications deleted; device
  tokens deactivated.
- private files owned (ID photos...) deleted from the bucket, except receipts / issue
  photos still attached to kept orders; public uploads (review photos) stay published.
- one audit_log row.
"""

import logging
import uuid
from typing import Any

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ApiError
from app.models import (
    AuditLog,
    Courier,
    DeviceToken,
    EmailCode,
    File,
    HotDeal,
    Message,
    Notification,
    Order,
    OrderIssue,
    OrderOffer,
    OrderStop,
    OrderTracking,
    User,
    UserAddress,
)
from app.realtime.events import emit
from app.security.deps import CurrentUser
from app.security.tokens import now_utc, revoke_all_for_user
from app.storage import s3

log = logging.getLogger("odsd.account_deletion")

# Every Order.status that is not terminal (delivered, cancelled).
ACTIVE_STATUSES = (
    "pending", "offers_received", "accepted", "at_shop", "price_confirmation_needed", "purchased",
    "on_the_way", "client_no_response",
)  # fmt: skip
DELETED_MESSAGE = "[message supprimé]"
DELETED_COURIER_NAME = "Livreur supprimé"
DELETED_CUSTOMER_NAME = "Client supprimé"
DELETED_USER_NAME = "Utilisateur supprimé"
DELETED_ADDRESS = "[adresse supprimée]"


def deleted_email(user_id: uuid.UUID) -> str:
    return f"deleted+{user_id.hex}@deleted.invalid"


async def _refuse_if_active(session: AsyncSession, user: User, courier: Courier | None) -> None:
    parties = [Order.customer_id == user.id]
    if courier is not None:
        parties.append(Order.courier_id == courier.id)
    active = (
        (
            await session.execute(
                select(Order.id)
                .where(or_(*parties), Order.status.in_(ACTIVE_STATUSES))
                .order_by(Order.created_at)
            )
        )
        .scalars()
        .all()
    )
    if active:
        raise ApiError(409, "order_in_progress", "An order is in progress", orders=[str(i) for i in active])


async def _anonymize_courier(session: AsyncSession, courier: Courier, counts: dict[str, int]) -> list[str]:
    """Returns the private keys to delete (ID photo)."""
    offers = (
        await session.execute(
            delete(OrderOffer)
            .where(OrderOffer.courier_id == courier.id, OrderOffer.status != "accepted")
            .returning(OrderOffer.id, OrderOffer.order_id)
        )
    ).all()
    counts["offers"] = len(offers)
    for offer_id, _order_id in offers:
        emit(session, "OrderOffer", "delete", offer_id, audience=[courier.user_id])
    await session.execute(delete(OrderTracking).where(OrderTracking.courier_id == courier.id))
    expired = (
        await session.execute(
            update(HotDeal)
            .where(HotDeal.courier_id == courier.id, HotDeal.status == "available")
            .values(status="expired")
            .returning(HotDeal.id)
        )
    ).scalars()
    for deal_id in expired:
        emit(session, "ResaleOrder", "update", deal_id)
    delivered = (
        (await session.execute(select(Order.id).where(Order.courier_id == courier.id))).scalars().all()
    )
    counts["courier_orders_anonymised"] = len(delivered)
    for order_id in delivered:
        emit(session, "Order", "update", order_id)

    keys = [courier.id_document_key] if courier.id_document_key else []
    courier.display_name = DELETED_COURIER_NAME
    courier.phone_e164 = None
    courier.id_document_number = ""
    courier.id_document_key = None
    courier.is_online = False
    courier.last_location = None
    courier.last_seen_at = None
    courier.referral_code = None
    courier.service_governorate = courier.service_city = None
    courier.verification = "rejected"
    courier.rejection_reason = "account_deleted"
    counts["courier_profiles"] = 1
    emit(session, "CourierProfile", "update", courier.id)
    return keys


async def _anonymize_customer_orders(session: AsyncSession, user: User, counts: dict[str, int]) -> None:
    orders = (
        await session.execute(
            update(Order)
            .where(Order.customer_id == user.id)
            .values(
                contact_name=DELETED_CUSTOMER_NAME,
                contact_phone_e164=None,
                delivery_address=DELETED_ADDRESS,
                delivery_details=None,
                delivery_location=None,
                notes=None,
            )
            .returning(Order.id)
        )
    ).scalars()
    ids = list(orders)
    counts["customer_orders"] = 0  # Base44 deleted them; they are kept (anonymized) now
    counts["customer_orders_anonymised"] = len(ids)
    for order_id in ids:
        emit(session, "Order", "update", order_id)


async def _private_files(session: AsyncSession, user: User, extra_keys: list[str]) -> list[str]:
    """Private objects to delete: owned files not attached to a kept order, + extra keys."""
    kept = (
        select(OrderStop.receipt_key)
        .where(OrderStop.receipt_key.is_not(None))
        .union(select(OrderIssue.photo_key).where(OrderIssue.photo_key.is_not(None)))
    )
    owned = (
        await session.execute(
            delete(File)
            .where(File.owner_id == user.id, File.visibility == "private", File.key.not_in(kept))
            .returning(File.key)
        )
    ).scalars()
    return sorted({*owned, *extra_keys})


async def delete_account(session: AsyncSession, actor: CurrentUser) -> tuple[dict[str, Any], list[str]]:
    """Anonymizes the account in the session's transaction. Returns (answer, private keys
    to delete from the bucket once the transaction is safe to commit)."""
    user = await session.get(User, actor.id, with_for_update=True)
    if user is None or user.deleted_at is not None:
        raise ApiError(404, "not_found", "Account not found")
    courier = (
        await session.execute(select(Courier).where(Courier.user_id == user.id).with_for_update())
    ).scalar_one_or_none()
    await _refuse_if_active(session, user, courier)

    counts: dict[str, int] = {
        "offers": 0, "device_tokens": 0, "notifications": 0, "customer_orders": 0, "messages_redacted": 0,
        "courier_orders_anonymised": 0, "courier_profiles": 0, "user_profiles": 0,
        "customer_orders_anonymised": 0, "private_files": 0,
    }  # fmt: skip
    extra_keys = await _anonymize_courier(session, courier, counts) if courier is not None else []
    counts["device_tokens"] = len(
        (
            await session.execute(
                update(DeviceToken)
                .where(DeviceToken.user_id == user.id, DeviceToken.is_active)
                .values(is_active=False, last_error="account_deleted")
                .returning(DeviceToken.id)
            )
        ).all()
    )
    notifications = (
        await session.execute(
            delete(Notification).where(Notification.user_id == user.id).returning(Notification.id)
        )
    ).scalars()
    for notification_id in notifications:
        counts["notifications"] += 1
        emit(session, "Notification", "delete", notification_id, audience=[user.id])
    counts["messages_redacted"] = len(
        (
            await session.execute(
                update(Message)
                .where(Message.sender_id == user.id, Message.body != DELETED_MESSAGE)
                .values(body=DELETED_MESSAGE)
                .returning(Message.id)
            )
        ).all()
    )
    await _anonymize_customer_orders(session, user, counts)
    keys = await _private_files(session, user, extra_keys)
    counts["private_files"] = len(keys)

    if user.profile_created_at is not None:
        counts["user_profiles"] = 1
        emit(session, "UserProfile", "delete", user.id, audience=[user.id])
    await session.execute(delete(UserAddress).where(UserAddress.user_id == user.id))
    await session.execute(delete(EmailCode).where(EmailCode.user_id == user.id))
    await revoke_all_for_user(session, user.id)
    now = now_utc()
    user.email = deleted_email(user.id)
    user.email_verified_at = None
    user.full_name = DELETED_USER_NAME
    user.phone_e164 = None
    user.password_hash = None
    user.google_sub = None
    user.profile_created_at = None
    user.whatsapp_opt_in_at = None
    user.push_enabled = False
    user.deleted_at = now
    user.disabled_at = now
    session.add(
        AuditLog(
            actor_user_id=user.id,
            action="account_deleted",
            entity="User",
            entity_id=str(user.id),
            after=counts,
        )
    )
    await session.flush()
    return {"success": True, "login_deleted": True, "deleted": counts}, keys


async def delete_private_objects(keys: list[str]) -> int:
    """Best effort: an unreachable bucket must not keep the account alive. Returns failures."""
    failures = 0
    for key in keys:
        try:
            await s3.delete_object(key)
        except Exception:
            failures += 1
            log.warning("could not delete private object %s", key)
    return failures
