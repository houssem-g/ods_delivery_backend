"""The courier's delivery steps, written by the app as `Order.update` (CourierOrderActive
handleStatusUpdate through MultiShopStatusButton), and the customer's geocode write
(OrderTracking). OWN_BACKEND_CLIENT.md §7 rows 16-20.

What the courier's app sends and what the server keeps of it:
- `status`: only the next step of COURIER_STEPS (accepted → at_shop → purchased →
  on_the_way → delivered; a hot deal goes accepted → on_the_way), or the same status
  with shop progress (multi-shop "arrived at shop 2");
- `shops[]`: per stop only `status` (forward only), `purchase_amount` (0 < x ≤ 2000, when
  the stop is bought), `receipt_photo_url` (one of our public uploads); names, addresses
  and positions are not the courier's to change;
- `purchase_amount` / `receipt_photo_url` (single shop, when buying);
- ignored, computed here: `current_shop_index`, `total_amount`, `status_history` (only the
  courier's lat/lng of its last entry is kept on the event), `platform_fee`,
  `courier_net_earning`, `ods_commission`, `ods_commission_status`.
Any other Order field in the body → 403, as for every other caller (the legacy fallbacks).
While a stock check waits for the customer (app/services/stock_checks.py) the order stays in
price_confirmation_needed: any step out of it → 409 stock_check_pending.
- `picked_items` (Aurora basket checklist): the indexes of the items_text lines already in the
  basket, unique ints 0..199, at most 200; written while accepted / at_shop /
  price_confirmation_needed (also while a stock check waits), 403 afterwards unless unchanged.
The customer's notice of each step (at_shop on arrival, purchased, on_the_way, delivered) is sent
here, after the transition (app/services/step_notices.py): the installed apps still send it through
sendNotificationIfEnabled after the write, which skips the duplicate.
"""

from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.errors import ApiError
from app.models import File, Order, OrderStop
from app.security.deps import CurrentUser
from app.services import order_transitions as ot
from app.services import step_notices, stock_checks
from app.services.geo import ORDER_BOUNDS, as_float, point, within

COURIER_STEPS: dict[str, frozenset[str]] = {
    "accepted": frozenset({"at_shop", "on_the_way"}),  # on_the_way: hot deal only
    "at_shop": frozenset({"price_confirmation_needed", "purchased"}),
    "price_confirmation_needed": frozenset({"at_shop", "purchased"}),
    "purchased": frozenset({"on_the_way"}),
    "on_the_way": frozenset({"delivered"}),
}
SHOPPING = ("accepted", "at_shop", "price_confirmation_needed")
STOP_RANK = {"pending": 0, "en_route": 1, "at_shop": 2, "purchased": 3}
MAX_PURCHASE = Decimal("2000")

COURIER_FIELDS = frozenset(
    {
        "status",
        "shops",
        "current_shop_index",
        "purchase_amount",
        "receipt_photo_url",
        "total_amount",
        "status_history",
        "platform_fee",
        "courier_net_earning",
        "ods_commission",
        "ods_commission_status",
        "picked_items",
    }
)
PICKED_MAX = 200  # entries of picked_items (indexes of items_text lines, 0..199)
CUSTOMER_FIELDS = frozenset({"shop_lat", "shop_lng", "delivery_lat", "delivery_lng"})
BUILTINS = frozenset({"id", "created_date", "updated_date", "created_by"})


def denied(message: str = "Permission denied for update operation on Order") -> ApiError:
    return ApiError(403, "permission_denied", message)


def bad(message: str) -> ApiError:
    return ApiError(400, "validation_error", message)


def refuse_foreign_fields(data: dict[str, Any], allowed: frozenset[str], order_fields: set[str]) -> None:
    extra = {k for k in data if k in order_fields and k not in allowed and k not in BUILTINS}
    if extra:
        raise denied(f"Permission denied for update operation on Order ({', '.join(sorted(extra))})")


def _amount(value: Any, name: str) -> Decimal:
    number = as_float(value)
    if number is None or number <= 0 or Decimal(str(number)) > MAX_PURCHASE:
        raise bad(f"{name}: expected an amount between 0 and {MAX_PURCHASE}")
    return Decimal(str(round(number, 3)))


def public_key_of(url: Any) -> str | None:
    """Bucket key of one of our public uploads, from its URL."""
    if not isinstance(url, str) or not url:
        return None
    prefix = f"{settings.public_files_base_url}/"
    if not url.startswith(prefix) or not url[len(prefix) :].startswith("public/"):
        raise bad("receipt_photo_url: not an uploaded file")
    return url[len(prefix) :]


async def _receipt_key(session: AsyncSession, url: Any, user: CurrentUser) -> str | None:
    """The receipt is the courier's own upload: a private file URI (current app, never
    exposed back) or, from older app builds, one of our public URLs."""
    if isinstance(url, str) and url.startswith("private/"):
        key: str | None = url
    else:
        key = public_key_of(url)
    if key is None:
        return None
    owner = (await session.execute(select(File.owner_id).where(File.key == key))).first()
    if owner is None or owner[0] != user.id:
        raise bad("receipt_photo_url: not an uploaded file")
    return key


def _location(history: Any) -> tuple[float, float] | None:
    """The courier's position the app puts in the last status_history entry."""
    if not isinstance(history, list) or not history or not isinstance(history[-1], dict):
        return None
    lat, lng = as_float(history[-1].get("lat")), as_float(history[-1].get("lng"))
    if lat is None or lng is None or not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    return lat, lng


async def _apply_shops(session: AsyncSession, user: CurrentUser, stops: list[OrderStop], shops: Any) -> bool:
    if not isinstance(shops, list) or len(shops) != len(stops):
        raise bad("shops: expected one entry per shop of the order")
    changed = False
    now = ot.now_utc()
    for stop, raw in zip(stops, shops, strict=True):
        if not isinstance(raw, dict):
            raise bad("shops: expected objects")
        new_status = raw.get("status", stop.status)
        if new_status not in STOP_RANK or stop.status not in STOP_RANK:
            if new_status == stop.status:
                continue
            raise bad("shops[].status: unknown status")
        if STOP_RANK[new_status] < STOP_RANK[stop.status]:
            raise denied("A shop step cannot go backwards")
        if new_status == stop.status:
            continue
        if new_status == "purchased":
            stop.purchase_amount = _amount(raw.get("purchase_amount"), "shops[].purchase_amount")
            stop.receipt_key = await _receipt_key(session, raw.get("receipt_photo_url"), user)
            stop.completed_at = now
        stop.status = new_status
        changed = True
    return changed


def _picked_items(value: Any) -> list[int]:
    """The indexes of the items_text lines already in the basket: unique ints 0..199, ≤ 200."""
    if not isinstance(value, list) or len(value) > PICKED_MAX:
        raise bad(f"picked_items: expected a list of at most {PICKED_MAX} line indexes")
    if any(isinstance(v, bool) or not isinstance(v, int) or not 0 <= v < PICKED_MAX for v in value):
        raise bad(f"picked_items: expected integers between 0 and {PICKED_MAX - 1}")
    if len(set(value)) != len(value):
        raise bad("picked_items: duplicate index")
    return list(value)


def _apply_picked(order: Order, data: dict[str, Any]) -> bool:
    """The courier's basket (Aurora checklist), while shopping only. True when it changed."""
    if "picked_items" not in data:
        return False
    raw = data["picked_items"]
    picked = None if raw is None else _picked_items(raw)
    if picked == order.picked_items:
        return False  # the app spreads the whole order into its writes
    if order.status not in SHOPPING:
        raise denied("picked_items can only change while shopping")
    order.picked_items = picked
    return True


def _moves_a_stop(stops: list[OrderStop], shops: Any) -> bool:
    if not isinstance(shops, list) or len(shops) != len(stops):
        return True
    return any(
        isinstance(raw, dict) and raw.get("status", s.status) != s.status
        for s, raw in zip(stops, shops, strict=True)
    )


def _current_seq(stops: list[OrderStop]) -> int:
    waiting = [s.seq for s in stops if s.status != "purchased"]
    return min(waiting) if waiting else max(s.seq for s in stops)


async def courier_step(
    session: AsyncSession, user: CurrentUser, order: Order, data: dict[str, Any], order_fields: set[str]
) -> None:
    """Applies the assigned courier's write (order locked by the caller)."""
    refuse_foreign_fields(data, COURIER_FIELDS, order_fields)
    from_status = order.status
    to_status = data.get("status", from_status)
    if not isinstance(to_status, str) or to_status not in ot.ALLOWED:
        raise bad("status: unknown status")
    stops = list(
        (
            await session.execute(
                select(OrderStop)
                .where(OrderStop.order_id == order.id)
                .order_by(OrderStop.seq)
                .with_for_update()
            )
        ).scalars()
    )
    if to_status != from_status and to_status not in COURIER_STEPS.get(from_status, frozenset()):
        raise denied(f"Status change from {from_status} to {to_status} is not allowed")
    # Waiting for the customer's stock-check answer: nothing moves (ticking basket lines does).
    moves = to_status != from_status or (
        "shops" in data and len(stops) > 1 and _moves_a_stop(stops, data["shops"])
    )
    if (
        from_status == "price_confirmation_needed"
        and moves
        and await stock_checks.has_pending(session, order.id)
    ):
        raise ApiError(409, "stock_check_pending", "The customer has not answered the stock check yet")
    hot_deal = order.resale_deal_id is not None
    if from_status == "accepted" and to_status == "on_the_way" and not hot_deal:
        raise denied(f"Status change from {from_status} to {to_status} is not allowed")

    picked_changed = _apply_picked(order, data)
    shops_changed = False
    if "shops" in data and len(stops) > 1:
        if from_status not in SHOPPING and _moves_a_stop(stops, data["shops"]):
            raise denied("The shops can only change while shopping")
        if from_status in SHOPPING:
            shops_changed = await _apply_shops(session, user, stops, data["shops"])
    if to_status == from_status:
        if not shops_changed and not picked_changed:
            return  # nothing the courier may change (a repeated tap): a no-op, like Base44
        if shops_changed and from_status not in ("at_shop", "price_confirmation_needed"):
            raise denied("The shops can only change while shopping")

    single = len(stops) <= 1
    if single and stops:
        stop = stops[0]
        if to_status in ("at_shop", "price_confirmation_needed") and stop.status in ("pending", "en_route"):
            stop.status = "at_shop"
        if to_status == "purchased":
            stop.purchase_amount = _amount(data.get("purchase_amount"), "purchase_amount")
            stop.receipt_key = await _receipt_key(session, data.get("receipt_photo_url"), user)
            stop.status, stop.completed_at = "purchased", ot.now_utc()
    if stops:
        if to_status == "purchased" and any(s.status != "purchased" for s in stops):
            raise bad("purchased: every shop of the order must be bought first")
        bought = [s.purchase_amount for s in stops if s.purchase_amount is not None]
        if bought:
            total = sum(bought, Decimal("0"))
            if total > MAX_PURCHASE:
                raise bad(f"purchase_amount: the shops total more than {MAX_PURCHASE}")
            order.purchase_amount = total
        order.current_stop_seq = _current_seq(stops)
    await session.flush()
    if to_status == from_status:
        return
    await ot.transition(
        session, order, to_status, user, "courier_app", location=_location(data.get("status_history"))
    )
    await step_notices.courier_step(session, order, from_status, to_status)


async def customer_geocode(
    session: AsyncSession, order: Order, data: dict[str, Any], order_fields: set[str]
) -> None:
    """OrderTracking geocodes an order placed without coordinates: the delivery point only,
    only while it is empty (placeOrder always sets it now; shop_* are ignored)."""
    refuse_foreign_fields(data, CUSTOMER_FIELDS, order_fields)
    lat, lng = as_float(data.get("delivery_lat")), as_float(data.get("delivery_lng"))
    if order.delivery_location is not None or not within(lat, lng, ORDER_BOUNDS):
        return
    assert lat is not None and lng is not None
    order.delivery_location = point(lat, lng)
    await session.flush()
