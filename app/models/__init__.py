"""SQLAlchemy models. They mirror the Alembic migrations (checked by `alembic check` in CI)."""

from app.models.base import Base
from app.models.catalog import GeocodeCache, Place, Shop, ShopMenuItem, ShopReview
from app.models.identity import (
    Courier,
    CourierDocument,
    EmailCode,
    PhoneVerification,
    RefreshToken,
    User,
    UserAddress,
)
from app.models.incidents import HotDeal, NoResponseCase
from app.models.misc import (
    AppSetting,
    AuditLog,
    CourierLedgerEntry,
    CreditCashier,
    CreditTopup,
    CourierStatement,
    File,
    TranslationUsage,
)
from app.models.notifications import DeviceToken, Notification, OutboundMessage, PushDelivery
from app.models.orders import (
    Message,
    MessageTranslation,
    OfferIntent,
    Order,
    OrderDraft,
    OrderIssue,
    OrderOffer,
    OrderRating,
    OrderStatusEvent,
    OrderStockCheck,
    OrderStop,
    OrderTracking,
)
from app.models.safety import UserBlock, UserReport
from app.models.views import courier_stats, customer_stats

__all__ = [
    "AppSetting",
    "AuditLog",
    "Base",
    "Courier",
    "CourierDocument",
    "CourierLedgerEntry",
    "CreditCashier",
    "CreditTopup",
    "CourierStatement",
    "DeviceToken",
    "EmailCode",
    "File",
    "GeocodeCache",
    "HotDeal",
    "Message",
    "MessageTranslation",
    "NoResponseCase",
    "Notification",
    "OfferIntent",
    "Order",
    "OrderDraft",
    "OrderIssue",
    "OrderOffer",
    "OrderRating",
    "OrderStatusEvent",
    "OrderStockCheck",
    "OrderStop",
    "OrderTracking",
    "OutboundMessage",
    "PhoneVerification",
    "Place",
    "PushDelivery",
    "RefreshToken",
    "Shop",
    "ShopMenuItem",
    "ShopReview",
    "TranslationUsage",
    "User",
    "UserAddress",
    "UserBlock",
    "UserReport",
    "courier_stats",
    "customer_stats",
]
