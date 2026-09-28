"""Importing this package registers every compat entity (one module per legacy entity)."""

from app.compat.entities import (
    app_settings,
    courier_profile,
    delivery_tariffs,
    device_token,
    message,
    message_log,
    no_response_case,
    notification,
    order,
    order_offer,
    place_index,
    resale_order,
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
    "no_response_case",
    "notification",
    "order",
    "order_offer",
    "place_index",
    "resale_order",
    "shop",
    "shop_review",
    "user_profile",
]
