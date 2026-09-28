"""Importing this package registers every compat entity (one module per legacy entity)."""

from app.compat.entities import (
    app_settings,
    courier_profile,
    delivery_tariffs,
    device_token,
    message,
    message_log,
    notification,
    order,
    order_offer,
    place_index,
    shop,
    shop_review,
    user_profile,
)

__all__ = [
    "app_settings",
    "courier_profile",
    "delivery_tariffs",
    "device_token",
    "message",
    "message_log",
    "notification",
    "order",
    "order_offer",
    "place_index",
    "shop",
    "shop_review",
    "user_profile",
]
