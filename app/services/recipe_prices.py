"""Ingredient prices for the Recettes, read from ODS — and the honest cost of a shopping list.

Source: ods-be `GET /api/v1/market/recipe-prices?country=TN` (contract: ods-audit/recettes-delivery-2110/
API-PRIX.md), signed like our webhooks to ODS (`X-ODSD-Timestamp` + `X-ODSD-Signature`, HMAC-SHA256 of
"<ts>." + body, a service secret `ODS_RECIPES_SECRET`). ODS applies every price rule (real sources only,
double check under 1 DT, no wholesale price, no courier ticket); Delivery only adds up.

Cache: the whole answer is kept per country in `recipe_price_cache` (prices change at most 3 times a week).
It is read again after `RECIPE_PRICES_TTL_MINUTES`, at most once per `RETRY_AFTER` when ODS fails. When ODS
does not answer and nothing was ever read, the screens show no cost at all — never an invented one.

Why not `POST /recipe-cost` per screen: the list of 40 recipe cards would need 40 calls (ODS allows 600 an
hour); the same rules are applied here from the cached prices (`rules` of the answer: 70 % priced by
lines AND by weight, else no total; range rounded outward to 0,5 DT; budget = known high + 15 %).
"""

from __future__ import annotations

import logging
import math
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import transaction
from app.models import RecipePriceCache
from app.services import partner_webhooks
from app.services.recipe_units import Ing, weight_kg

log = logging.getLogger("odsd.recipe_prices")

PATH = "/api/v1/market/recipe-prices"
RETRY_AFTER = timedelta(minutes=2)
DEFAULT_RULES = {"min_priced_share": 0.7, "stale_days": 14, "rounding_tnd": 0.5}
BUDGET_MARGIN = Decimal("1.15")  # « plafond = haut de la fourchette des lignes avec prix + 15 % »
PRICE_UNIT = {"kg": "kg", "l": "l", "piece": "piece", "botte": "botte"}

_transport: httpx.AsyncBaseTransport | None = None  # tests replace the network


def set_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    global _transport
    _transport = transport


def configured() -> bool:
    return bool(settings.ODS_API_BASE_URL and settings.ODS_RECIPES_SECRET)


async def _fetch(country: str) -> dict[str, Any] | None:
    ts = str(int(time.time()))
    headers = {
        "X-ODSD-Timestamp": ts,
        "X-ODSD-Signature": partner_webhooks.sign(settings.ODS_RECIPES_SECRET or "", ts, b""),
        "Accept": "application/json",
    }
    url = f"{settings.ODS_API_BASE_URL.rstrip('/')}{PATH}"
    try:
        async with httpx.AsyncClient(
            timeout=settings.RECIPE_PRICES_TIMEOUT_SECONDS, transport=_transport
        ) as client:
            response = await client.get(url, params={"country": country}, headers=headers)
    except httpx.HTTPError as exc:
        log.warning("recipe prices: ODS unreachable (%s)", type(exc).__name__)
        return None
    if response.status_code != 200:
        log.warning("recipe prices: ODS answered %s", response.status_code)
        return None
    try:
        data = response.json()
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("ingredients"), list):
        return None
    return data


async def prices(session: AsyncSession, country: str = "TN") -> dict[str, Any] | None:
    """The cached ODS answer, read again when old (None: never read and ODS does not answer)."""
    row = await session.get(RecipePriceCache, country)
    now = datetime.now(UTC)
    fresh = row is not None and now - row.fetched_at < timedelta(minutes=settings.RECIPE_PRICES_TTL_MINUTES)
    recently_tried = row is not None and row.tried_at is not None and now - row.tried_at < RETRY_AFTER
    if fresh or recently_tried or not configured():
        return row.payload if row is not None else None
    data = await _fetch(country)
    async with transaction() as own:  # kept even if the caller's request rolls back
        cached = await own.get(RecipePriceCache, country)
        if data is not None:
            if cached is None:
                own.add(RecipePriceCache(country=country, payload=data, fetched_at=now, tried_at=now))
            else:
                cached.payload, cached.fetched_at, cached.tried_at = data, now, now
        elif cached is not None:
            cached.tried_at = now
    if row is not None:
        await session.refresh(row)
        return row.payload
    return data


def price_index(payload: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """{ingredient key: its `price` object} for the priced ones."""
    out: dict[str, dict[str, Any]] = {}
    for item in (payload or {}).get("ingredients") or []:
        if isinstance(item, dict) and isinstance(item.get("price"), dict) and item.get("key"):
            out[str(item["key"])] = item["price"]
    return out


def _round_down(x: float, step: float) -> float:
    return math.floor(x / step + 1e-9) * step


def _round_up(x: float, step: float) -> float:
    return math.ceil(x / step - 1e-9) * step


def _num(value: Any) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and v >= 0 else None


def cost(
    lines: list[tuple[Ing, Decimal]], payload: dict[str, Any] | None, today: date | None = None
) -> dict[str, Any]:
    """The honest cost of what is bought: [(ingredient, quantity in its buying unit, rounded)].

    → {available, show_total, low, high, known_low, known_high, budget_max, priced, unpriced[], lines{id: …},
       oldest_as_of, stale, to_check, sources[]}. `available: false` = no price list at all (ODS never read):
    the screen shows no cost. `low` / `high` are None unless `show_total`."""
    if not payload:
        return {"available": False}
    rules = {**DEFAULT_RULES, **(payload.get("rules") if isinstance(payload.get("rules"), dict) else {})}
    step = float(rules.get("rounding_tnd") or 0.5)
    share_min = float(rules.get("min_priced_share") or 0.7)
    stale_days = int(rules.get("stale_days") or 14)
    index = price_index(payload)
    today = today or date.today()
    low = high = 0.0
    w_total = w_priced = 0.0
    priced_n = 0
    unpriced: list[dict[str, Any]] = []
    per_line: dict[int, dict[str, Any]] = {}
    oldest: date | None = None
    to_check = False
    sources: list[str] = []
    for ing, qty in lines:
        q = float(qty)
        w = weight_kg(qty, ing.unit, ing)
        if w is not None:
            w_total += float(w)
        price = index.get(ing.key)
        lo_u, hi_u = (_num(price.get("min")), _num(price.get("max"))) if price else (None, None)
        if price is None or lo_u is None or PRICE_UNIT.get(ing.unit) != price.get("unit"):
            unpriced.append({"id": ing.id, "fr": ing.name_fr, "ar": ing.name_ar})
            per_line[ing.id] = {"priced": False}
            continue
        hi_u = max(hi_u if hi_u is not None else lo_u, lo_u)
        lo, hi = q * lo_u, q * hi_u
        low, high = low + lo, high + hi
        priced_n += 1
        if w is not None:
            w_priced += float(w)
        as_of = None
        try:
            as_of = date.fromisoformat(str(price.get("as_of"))[:10])
        except ValueError:
            as_of = None
        if as_of is not None and price.get("method") != "official" and (oldest is None or as_of < oldest):
            oldest = as_of
        to_check = to_check or price.get("to_check") is True
        source = str(price.get("source") or "").strip()
        if source and source not in sources:
            sources.append(source)
        per_line[ing.id] = {
            "priced": True,
            "low": round(lo, 3),
            "high": round(hi, 3),
            "as_of": as_of.isoformat() if as_of else None,
            "stale": price.get("stale") is True,
            "to_check": price.get("to_check") is True,
            "source": source or None,
        }
    n = priced_n + len(unpriced)
    share_lines = priced_n / n if n else 0.0
    share_weight = w_priced / w_total if w_total else share_lines
    show = n > 0 and share_lines >= share_min and share_weight >= share_min
    known_low = _round_down(low, step) if priced_n else None
    known_high = _round_up(high, step) if priced_n else None
    budget = None
    if show and known_high is not None:
        budget = math.ceil(float(Decimal(str(known_high)) * BUDGET_MARGIN) - 1e-9)
    return {
        "available": True,
        "show_total": show,
        "low": known_low if show else None,
        "high": known_high if show else None,
        "known_low": known_low,
        "known_high": known_high,
        "budget_max": budget,
        "priced": priced_n,
        "unpriced": unpriced,
        "share_lines": round(share_lines, 3),
        "share_weight": round(share_weight, 3),
        "oldest_as_of": oldest.isoformat() if oldest else None,
        "stale": bool(oldest and (today - oldest).days > stale_days),
        "to_check": to_check,
        "sources": sources,
        "lines": per_line,
    }


def per_person(c: dict[str, Any], servings: int) -> dict[str, Any] | None:
    """The card's « ≈ 4 à 4.500 DT / pers. » (only when a total is shown)."""
    if not c.get("show_total") or not servings:
        return None
    return {"low": round(c["low"] / servings, 3), "high": round(c["high"] / servings, 3)}
