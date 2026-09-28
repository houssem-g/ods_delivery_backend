"""Helpers shared by transform / import / verify: stable ids, dates, money, phones, geo, masking.

Nothing here touches the network or the database.
"""

import re
import uuid
from datetime import UTC, datetime, time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

import phonenumbers

from app.services.phones import InvalidPhone, to_e164

# Every migrated row gets uuid5(NAMESPACE, "<Entity>:<legacy id>"): reruns produce the same ids.
NAMESPACE = uuid.UUID("0d5d0c1e-7b44-4d8e-9a55-0de11ce0b44a")
B44_ID = re.compile(r"^[0-9a-f]{24}$")
MILLIME = Decimal("0.001")


def det_uuid(entity: str, legacy_id: str) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, f"{entity}:{legacy_id}")


def is_b44_id(value: Any) -> bool:
    return isinstance(value, str) and bool(B44_ID.match(value))


def blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def text_or_none(value: Any, limit: int | None = None) -> str | None:
    """'' and whitespace -> None; other values as stripped text."""
    if blank(value):
        return None
    out = str(value).strip()
    return out[:limit] if limit else out


def parse_dt(value: Any) -> datetime | None:
    """Base44 dates: naive UTC ('2026-09-28T08:31:58.996000'), with 'Z' (User, *_at fields)
    or without microseconds. Always returns an aware UTC datetime."""
    if blank(value):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    raw = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def parse_hhmm(value: Any) -> time | None:
    if blank(value):
        return None
    match = re.fullmatch(r"(\d{1,2}):(\d{2})(?::\d{2})?", str(value).strip())
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    return time(hour, minute) if hour < 24 and minute < 60 else None


def money(value: Any) -> Decimal | None:
    """Float/int -> Decimal rounded to the millime (numeric(10,3)); None for blank / non numbers."""
    if value is None or isinstance(value, bool) or blank(value):
        return None
    try:
        return Decimal(str(value)).quantize(MILLIME, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return None


def number(value: Any, places: int) -> Decimal | None:
    if value is None or isinstance(value, bool) or blank(value):
        return None
    try:
        return Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return None


def integer(value: Any) -> int | None:
    if value is None or isinstance(value, bool) or blank(value):
        return None
    try:
        return round(float(value))
    except (TypeError, ValueError):
        return None


def point(lat: Any, lng: Any) -> str | None:
    """EWKT for a geography(Point,4326) column, or None when missing / out of range."""
    if lat is None or lng is None or isinstance(lat, bool) or isinstance(lng, bool):
        return None
    try:
        la, ln = float(lat), float(lng)
    except (TypeError, ValueError):
        return None
    if not (-90 <= la <= 90 and -180 <= ln <= 180) or (la == 0 and ln == 0):
        return None
    return f"SRID=4326;POINT({ln!r} {la!r})"


# --- phones -------------------------------------------------------------------------------

PHONE_OK, PHONE_BLANK, PHONE_FALLBACK, PHONE_REJECTED = "ok", "blank", "fallback_region", "rejected"


def normalize_phone(raw: Any, fallback_regions: tuple[str, ...] = ()) -> tuple[str | None, str]:
    """(E.164 or None, outcome). Tunisia first (8 digits -> +216, the app rule); a number that is
    not Tunisian is retried in `fallback_regions` (national format, e.g. a Swiss 07x mobile);
    a bare country code is blank; anything else is rejected (None)."""
    if raw is None or isinstance(raw, bool):
        return None, PHONE_BLANK
    text = str(raw).strip()
    if not text:
        return None, PHONE_BLANK
    try:
        e164 = to_e164(text)
    except InvalidPhone:
        e164 = None
        for region in fallback_regions:
            try:
                parsed = phonenumbers.parse(text, region)
            except phonenumbers.NumberParseException:
                continue
            if phonenumbers.is_valid_number(parsed):
                return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164), PHONE_FALLBACK
        return None, PHONE_REJECTED
    return (e164, PHONE_OK) if e164 else (None, PHONE_BLANK)


# --- places ---------------------------------------------------------------------------------


# The app's category ids (refreshOsmIndex CATEGORY_RULES keys, ShopSearch.jsx); the English
# values come from older OSM imports. `cafe` is in the restaurant rule (amenity=cafe).
CATEGORY_CANONICAL = (
    "restaurant",
    "pharmacie",
    "supermarché",
    "boulangerie",
    "banque",
    "carburant",
    "hôpital",
)
CATEGORY_SYNONYMS = {
    "pharmacy": "pharmacie",
    "supermarket": "supermarché",
    "supermarche": "supermarché",
    "grocery": "supermarché",
    "convenience": "supermarché",
    "bank": "banque",
    "bakery": "boulangerie",
    "hospital": "hôpital",
    "hopital": "hôpital",
    "clinic": "hôpital",
    "fuel": "carburant",
    "cafe": "restaurant",
    "café": "restaurant",
    "fast_food": "restaurant",
}


def unify_category(value: Any) -> str:
    key = str(value or "").strip().lower()
    return CATEGORY_SYNONYMS.get(key, key)


# --- masking (reports never show personal data in clear) ------------------------------------


def mask_email(value: Any) -> str:
    text = str(value or "")
    if "@" not in text:
        return mask_text(text)
    local, _, domain = text.partition("@")
    return f"{local[:3]}…@{domain}"


def mask_text(value: Any) -> str:
    text = str(value or "")
    if not text:
        return "∅"
    return f"{text[:2]}…({len(text)})"


def mask_phone(value: Any) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    return f"{len(digits)} digits, …{digits[-2:]}" if digits else "∅"
