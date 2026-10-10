"""The legacy functions: one module per function, named exactly like the
function (`getSupportContacts.py` answers POST /api/functions/getSupportContacts).

A module exposes:
    async def handle(payload: dict, user: CurrentUser | None, session: AsyncSession,
                     request: Request) -> tuple[int, dict]
    AUTH = "user" (default: 401 without a session) or "optional" (anonymous callers allowed)
"""

import importlib
import pkgutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.security.deps import CurrentUser

Handler = Callable[[dict[str, Any], CurrentUser | None, AsyncSession, Request], Awaitable[tuple[int, dict]]]

# Retired functions answer 410 (DB_AUDIT §4.3).
RETIRED = frozenset(
    {
        "calculateAdaptiveRadius", "createOrder", "getActiveTariffs", "getOrderOffers",
        "handleQuickResponse", "manageTestData", "notifyCourierNewOrder", "notifyOrderStatusChange",
        "notifyRouteDelay", "notifyShopNewOrder", "realtimeSubscribe", "searchShopsOSM",
        "sendCustomerOrderNotification", "sendEnhancedCourierNotification", "sendLocationNotification",
        "sendOrderNotification",
    }
)  # fmt: skip


@dataclass(frozen=True)
class FunctionDef:
    name: str
    handle: Handler
    auth: Literal["user", "optional"]


def discover() -> dict[str, FunctionDef]:
    found: dict[str, FunctionDef] = {}
    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{__name__}.{info.name}")
        auth = getattr(module, "AUTH", "user")
        if auth not in ("user", "optional"):
            raise RuntimeError(f"{info.name}: AUTH must be 'user' or 'optional'")
        if info.name in RETIRED:
            raise RuntimeError(f"{info.name} is retired and must not be ported")
        found[info.name] = FunctionDef(name=info.name, handle=module.handle, auth=auth)
    return found


FUNCTIONS = discover()
