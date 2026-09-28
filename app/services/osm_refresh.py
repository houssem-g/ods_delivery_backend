"""Overpass import into `places`, the port of refreshOsmIndex.

Per category: Tunisia split in 3 x 2 tiles, node + way + relation (`out center`),
failed tiles retried once, elements deduplicated by `type/id`, unnamed ones skipped,
then one bulk upsert on `osm_id` (Base44 upserted one row at a time with 429
back-off). Seven unified FR categories (the UI vocabulary): cafés are merged into
restaurant, ATMs into banque, groceries/markets into supermarché.

The Overpass calls happen before any database write, so no connection is held while
waiting for the network.
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import literal_column
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations import osm
from app.models import Place
from app.security.tokens import now_utc
from app.services import text_norm
from app.services.places import backfill_search_norm

log = logging.getLogger("odsd.osm_refresh")

TUNISIA_BBOX = (30.2, 7.5, 37.6, 11.6)  # south, west, north, east
GRID_ROWS, GRID_COLS = 3, 2
TILE_DELAY_SECONDS = 1.5
RETRY_PASS_DELAY_SECONDS = 8.0
UPSERT_BATCH = 500


@dataclass(frozen=True)
class CategoryRule:
    key: str
    tag_filters: tuple[str, ...]


CATEGORY_RULES: tuple[CategoryRule, ...] = (
    CategoryRule(
        "restaurant",
        (
            'amenity~"^(restaurant|fast_food|food_court|ice_cream|biergarten|bar|pub|cafe|internet_cafe)$"',
            'shop~"^(coffee|tea)$"',
        ),
    ),
    CategoryRule("pharmacie", ('amenity="pharmacy"', 'healthcare="pharmacy"', 'shop="chemist"')),
    CategoryRule(
        "supermarché",
        (
            'shop~"^(supermarket|convenience|department_store|mall|wholesale|kiosk|general|variety_store|'
            'butcher|greengrocer|seafood|deli|cheese|spices|dairy|nuts|beverages|alcohol|tobacco|farm)$"',
        ),
    ),
    CategoryRule("boulangerie", ('shop~"^(bakery|pastry|confectionery|chocolate)$"',)),
    CategoryRule("banque", ('amenity~"^(bank|atm|bureau_de_change)$"',)),
    CategoryRule("carburant", ('amenity="fuel"',)),
    CategoryRule(
        "hôpital",
        (
            'amenity~"^(hospital|clinic|doctors|dentist|veterinary)$"',
            'healthcare~"^(hospital|clinic|doctor|dentist|laboratory|physiotherapist|centre)$"',
        ),
    ),
)
RULES_BY_KEY = {rule.key: rule for rule in CATEGORY_RULES}
# JS getUTCDay(): 0 = Sunday
WEEKDAY_TO_CATEGORY = (
    "restaurant",
    "pharmacie",
    "supermarché",
    "boulangerie",
    "banque",
    "carburant",
    "hôpital",
)


class UnknownCategory(ValueError):
    pass


def targets_for(requested: str, today: datetime | None = None) -> list[CategoryRule]:
    """'all' → every category; a key → that one; '' → the weekday's (UTC) category."""
    requested = requested.strip()
    if requested == "all":
        return list(CATEGORY_RULES)
    if requested:
        rule = RULES_BY_KEY.get(requested)
        if rule is None:
            raise UnknownCategory(requested)
        return [rule]
    day = today or now_utc()
    return [RULES_BY_KEY[WEEKDAY_TO_CATEGORY[(day.weekday() + 1) % 7]]]


def bbox_grid(bbox: tuple[float, float, float, float], rows: int, cols: int) -> list[str]:
    south, west, north, east = bbox
    lat_step, lng_step = (north - south) / rows, (east - west) / cols
    tiles = []
    for r in range(rows):
        for c in range(cols):
            s = south + r * lat_step
            n = north if r == rows - 1 else south + (r + 1) * lat_step
            w = west + c * lng_step
            e = east if c == cols - 1 else west + (c + 1) * lng_step
            tiles.append(f"{s:g},{w:g},{n:g},{e:g}")
    return tiles


def build_query(rule: CategoryRule, tile: str) -> str:
    blocks = "\n".join(
        f"  node[{f}]({tile});\n  way[{f}]({tile});\n  relation[{f}]({tile});" for f in rule.tag_filters
    )
    return f"[out:json][timeout:60];\n(\n{blocks}\n);\nout center 5000;"


def format_address(tags: dict[str, Any]) -> str:
    parts = [tags.get(k) for k in ("addr:housenumber", "addr:street", "addr:city", "addr:postcode")]
    return ", ".join(str(p) for p in parts if p)


def governorate_of(tags: dict[str, Any]) -> str:
    for key in ("addr:state", "addr:province", "addr:region", "is_in"):
        if tags.get(key):
            return str(tags[key])
    return ""


def quality_score(tags: dict[str, Any]) -> int:
    """Deno qualityScore (0.5 + completeness bonuses, max 1) as a percent."""
    score = 50
    if tags.get("name"):
        score += 10
    if tags.get("addr:street") or tags.get("addr:housenumber"):
        score += 10
    for keys in (("phone", "contact:phone"), ("opening_hours",), ("website", "contact:website")):
        if any(tags.get(k) for k in keys):
            score += 5
    if tags.get("name:fr") or tags.get("name:ar"):
        score += 5
    return min(100, score)


def _name(tags: dict[str, Any]) -> str | None:
    for key in ("name", "name:fr", "name:ar", "name:en"):
        if tags.get(key):
            return str(tags[key])
    for key, value in tags.items():
        if key.startswith("name:") and value:
            return str(value)
    return None


def element_to_place(element: dict[str, Any], category: str, source_ts: datetime) -> dict[str, Any] | None:
    """Deno elementToPlace → a `places` row (None when unnamed or without coordinates)."""
    tags = element.get("tags") or {}
    center = element.get("center") or {}
    lat = element.get("lat", center.get("lat"))
    lng = element.get("lon", center.get("lon"))
    if not isinstance(lat, (int, float)) or not isinstance(lng, (int, float)):
        return None
    name = _name(tags)
    if not name:
        return None
    address = format_address(tags) or None
    city = str(tags.get("addr:city") or tags.get("addr:town") or tags.get("addr:village") or "") or None
    return {
        "osm_id": f"{element.get('type')}/{element.get('id')}",
        "name": name,
        "name_norm": text_norm.normalize_text(name),
        "search_norm": text_norm.search_text(name, address, city) or "-",
        "address": address,
        "city": city,
        "governorate": governorate_of(tags) or None,
        "category": category,
        "location": f"SRID=4326;POINT({float(lng)} {float(lat)})",
        "phone": str(tags.get("phone") or tags.get("contact:phone") or "") or None,
        "opening_hours": str(tags.get("opening_hours") or "") or None,
        "source": "osm",
        "source_ts": source_ts,
        "quality_score": quality_score(tags),
        "refreshed_at": source_ts,
    }


@dataclass
class CategoryResult:
    category: str
    fetched: int = 0
    parsed: int = 0
    created: int = 0
    updated: int = 0
    errors: int = 0
    failedTiles: int = 0  # legacy answer key (camelCase)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class FetchOutcome:
    elements: list[dict[str, Any]] = field(default_factory=list)
    failed_tiles: list[int] = field(default_factory=list)


async def fetch_category(rule: CategoryRule, log_lines: list[str]) -> FetchOutcome:
    tiles = bbox_grid(TUNISIA_BBOX, GRID_ROWS, GRID_COLS)
    outcome = FetchOutcome()
    log_lines.append(f"▶  {rule.key} — {len(rule.tag_filters)} filter(s)")
    for i, tile in enumerate(tiles):
        try:
            data = await osm.overpass_query(build_query(rule, tile))
            elements = data.get("elements") or []
            log_lines.append(f"   tile {i + 1}/{len(tiles)}: {len(elements)} elements")
            outcome.elements.extend(e for e in elements if isinstance(e, dict))
        except osm.OverpassError as exc:
            outcome.failed_tiles.append(i)
            log_lines.append(f"   tile {i + 1}/{len(tiles)}: FAILED ({exc})")
        await osm.pause(TILE_DELAY_SECONDS)
    if outcome.failed_tiles:
        log_lines.append(f"   ↻ retrying {len(outcome.failed_tiles)} failed tile(s)")
        await osm.pause(RETRY_PASS_DELAY_SECONDS)
        for i in list(outcome.failed_tiles):
            try:
                data = await osm.overpass_query(build_query(rule, tiles[i]))
                elements = data.get("elements") or []
                log_lines.append(f"   retry tile {i + 1}: {len(elements)} elements")
                outcome.elements.extend(e for e in elements if isinstance(e, dict))
                outcome.failed_tiles.remove(i)
            except osm.OverpassError as exc:
                log_lines.append(f"   retry tile {i + 1}: FAILED ({exc})")
            await osm.pause(TILE_DELAY_SECONDS * 2)
    return outcome


async def upsert_places(session: AsyncSession, rows: list[dict[str, Any]]) -> tuple[int, int]:
    """Bulk upsert on osm_id; returns (created, updated)."""
    created = 0
    for start in range(0, len(rows), UPSERT_BATCH):
        chunk = rows[start : start + UPSERT_BATCH]
        stmt = insert(Place).values(chunk)
        stmt = stmt.on_conflict_do_update(
            index_elements=[Place.osm_id],
            set_={key: stmt.excluded[key] for key in chunk[0] if key != "osm_id"},
        ).returning(literal_column("(xmax = 0)"))  # true ⇔ inserted by this statement
        created += sum(1 for inserted in (await session.execute(stmt)).scalars() if inserted)
    return created, len(rows) - created


async def refresh_category(session: AsyncSession, rule: CategoryRule, log_lines: list[str]) -> CategoryResult:
    fetched = await fetch_category(rule, log_lines)
    result = CategoryResult(category=rule.key, fetched=len(fetched.elements))
    result.failedTiles = len(fetched.failed_tiles)
    source_ts = now_utc()
    by_id: dict[str, dict[str, Any]] = {}
    for element in fetched.elements:
        by_id[f"{element.get('type')}/{element.get('id')}"] = element
    rows, unnamed = [], 0
    for element in by_id.values():
        row = element_to_place(element, rule.key, source_ts)
        if row is None:
            unnamed += 1
        else:
            rows.append(row)
    result.parsed = len(rows)
    log_lines.append(f"   parsed {len(rows)} named places ({unnamed} unnamed skipped)")
    if rows:
        try:
            async with session.begin_nested():
                result.created, result.updated = await upsert_places(session, rows)
        except Exception as exc:
            log.exception("osm upsert failed for %s", rule.key)
            result.errors = len(rows)
            log_lines.append(f"   ✗ upsert failed: {type(exc).__name__}")
    log_lines.append(f"   ✓ +{result.created} created, ~{result.updated} updated, {result.errors} errors")
    return result


async def run_refresh(session: AsyncSession, rules: list[CategoryRule]) -> dict[str, Any]:
    """The refreshOsmIndex answer: {success, duration_ms, results, log, backfilled}."""
    log_lines = [
        f"refreshOsmIndex started @ {now_utc().isoformat()}",
        f"Categories: {', '.join(rule.key for rule in rules)}",
    ]
    started = time.monotonic()
    results = []
    for rule in rules:
        results.append((await refresh_category(session, rule, log_lines)).as_dict())
    backfilled = await backfill_search_norm(session)
    if backfilled:
        log_lines.append(f"search columns computed for {backfilled} imported place(s)")
    return {
        "success": True,
        "duration_ms": round((time.monotonic() - started) * 1000),
        "results": results,
        "log": log_lines,
        "backfilled": backfilled,
    }
