"""Importing this package registers every compat entity (one module per legacy entity)."""

from app.compat.entities import (
    app_settings,
    device_token,
    message,
    message_log,
    notification,
    user_profile,
)

__all__ = ["app_settings", "device_token", "message", "message_log", "notification", "user_profile"]
