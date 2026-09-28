"""Base44 export (one JSON array per entity) -> normalized rows for our schema (docs/FIELD_MAPPING.md).

Pure and in memory: no database, no network. Every row carries its final uuid, computed as
uuid5(legacy id), so a rerun produces exactly the same rows and import.py can upsert them.
Every exclusion and every value changed on the way is counted in the Report (docs/MIGRATION.md
lists the rules and their reasons).

    uv run python -m migrate.transform EXPORT_DIR [--fallback-region CH] [--out DIR]
"""

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.models.incidents import NO_RESPONSE_RESOLUTIONS
from app.models.notifications import NOTIFICATION_TYPE_SYNONYMS, NOTIFICATION_TYPES
from app.services.shops import stored_photo
from app.services.text_norm import normalize_text, search_text
from app.storage.keys import IMAGE_TYPES, sniff_matches
from migrate.bundle import TABLE_ORDER, Bundle, FileToUpload, Report
from migrate.common import (
    PHONE_FALLBACK,
    PHONE_REJECTED,
    blank,
    det_uuid,
    integer,
    is_b44_id,
    mask_email,
    mask_phone,
    money,
    normalize_phone,
    number,
    parse_dt,
    parse_hhmm,
    point,
    text_or_none,
    unify_category,
)

ENTITIES = (
    "User", "UserProfile", "CourierProfile", "Order", "OrderOffer", "Message", "Notification",
    "DeviceToken", "ResaleOrder", "NoResponseCase", "MessageLog", "AppSettings", "DeliveryTariffs",
    "Shop", "ShopReview", "PlaceIndex",
)  # fmt: skip
ORDER_STATUSES = (
    "pending", "offers_received", "accepted", "at_shop", "price_confirmation_needed", "purchased",
    "on_the_way", "delivered", "cancelled", "client_no_response",
)  # fmt: skip
COURIER_REQUIRED = {
    "accepted", "at_shop", "price_confirmation_needed", "purchased", "on_the_way", "delivered",
    "client_no_response",
}  # fmt: skip
CANCELLED_BY = {"customer", "courier", "admin", "system"}
STOP_STATUSES = {"pending", "en_route", "at_shop", "purchased", "skipped"}
OFFER_STATUSES = {"pending", "accepted", "rejected", "expired", "withdrawn"}
PACKAGES = ("petit", "moyen", "grand")
VERIFICATIONS = ("pending", "verified", "rejected")
# ods-delivery src/constants/commission.js: 0.5 TND per delivery, waived until the launch end.
COMMISSION_PER_DELIVERY = Decimal("0.500")
LAUNCH_END = datetime(2026, 12, 31, 23, 0, tzinfo=UTC)  # 2027-01-01T00:00:00+01:00
MIGRATION_SOURCE = "migration"
AUDIT_ADJUST = "b44_migration.adjust"
AUDIT_EXCLUDE = "b44_migration.exclude"
# notification metadata keys holding legacy ids -> entity whose new id replaces them
METADATA_ID_KEYS = {
    "order_id": "Order",
    "offer_id": "OrderOffer",
    "courier_id": "CourierProfile",
    "case_id": "NoResponseCase",
    "resale_order_id": "ResaleOrder",
    "message_id": "Message",
}
EXT_TYPES = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}


def load_export(export_dir: Path) -> dict[str, list[dict[str, Any]]]:
    """Every entity file present (a missing file = no rows); refuses anything but JSON arrays."""
    data: dict[str, list[dict[str, Any]]] = {}
    for entity in ENTITIES:
        path = export_dir / f"{entity}.json"
        if not path.is_file():
            data[entity] = []
            continue
        rows = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(rows, list):
            raise ValueError(f"{path.name}: expected a JSON array")
        data[entity] = rows
    return data


def load_file_index(export_dir: Path) -> list[dict[str, Any]]:
    path = export_dir / "files" / "_index.json"
    if not path.is_file():
        return []
    entries = json.loads(path.read_text(encoding="utf-8"))
    return entries if isinstance(entries, list) else []


def _lower(value: Any) -> str:
    return str(value or "").strip().lower()


def _latest(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return max(
        rows, key=lambda r: (r.get("updated_date") or "", r.get("created_date") or "", r.get("id", ""))
    )


def _stamps(row: dict[str, Any]) -> dict[str, Any]:
    created = parse_dt(row.get("created_date")) or datetime(2026, 1, 1, tzinfo=UTC)
    updated = parse_dt(row.get("updated_date")) or created
    return {"created_at": created, "updated_at": updated}


class Transformer:
    def __init__(
        self,
        export: dict[str, list[dict[str, Any]]],
        *,
        export_dir: Path | None = None,
        file_index: list[dict[str, Any]] | None = None,
        fallback_regions: tuple[str, ...] = ("CH",),
        qa_emails: set[str] | None = None,
    ) -> None:
        self.export = export
        self.export_dir = export_dir
        self.file_index = file_index or []
        self.fallback_regions = fallback_regions
        self.qa_emails = {_lower(e) for e in (qa_emails or set()) if e}
        self.report = Report(export_counts={e: len(export.get(e, [])) for e in ENTITIES})
        self.tables: dict[str, list[dict[str, Any]]] = {t: [] for t in TABLE_ORDER}
        self.files: list[FileToUpload] = []
        self.kept: dict[str, set[str]] = defaultdict(set)
        # legacy -> new ids of the rows kept
        self.user_by_email: dict[str, Any] = {}
        self.user_by_legacy: dict[str, Any] = {}
        self.courier_by_legacy: dict[str, Any] = {}
        self.courier_user: dict[Any, Any] = {}  # courier uuid -> user uuid
        self.courier_by_user_email: dict[str, Any] = {}
        self.order_by_legacy: dict[str, Any] = {}
        self.order_rows: dict[str, dict[str, Any]] = {}  # legacy id -> orders row
        self.ids: dict[str, dict[str, Any]] = defaultdict(dict)  # entity -> legacy -> uuid

    # --- small helpers ------------------------------------------------------------------

    def audit(self, action: str, entity: str, entity_id: Any, before: dict, after: dict | None) -> None:
        self.tables["audit_log"].append(
            {
                "action": action,
                "entity": entity,
                "entity_id": str(entity_id),
                "before": _jsonable(before),
                "after": _jsonable(after) if after is not None else None,
            }
        )

    def phone(self, table: str, column: str, raw: Any) -> str | None:
        e164, outcome = normalize_phone(raw, self.fallback_regions)
        if outcome == PHONE_REJECTED:
            self.report.adjust(table, column, "phone rejected (not a number) -> NULL", mask_phone(raw))
        elif outcome == PHONE_FALLBACK:
            self.report.adjust(table, column, "phone read in a fallback region", mask_phone(raw))
        return e164

    def user_ref(self, value: Any) -> Any:
        """A Base44 user reference: an e-mail, or (known defect) a CourierProfile id."""
        if is_b44_id(value):
            courier = self.courier_by_legacy.get(value)
            return self.courier_user.get(courier) if courier else self.user_by_legacy.get(value)
        return self.user_by_email.get(_lower(value))

    def local_file(self, name: str) -> Path | None:
        if self.export_dir is None:
            return None
        path = self.export_dir / "files" / name
        return path if path.is_file() else None

    def add_file(
        self,
        entry: dict[str, Any],
        key_for: Any,
        owner: Any,
        visibility: str,
        purpose: str,
        created: datetime,
    ) -> str | None:
        """Registers an exported file for upload and a `files` row; returns its bucket key."""
        path = self.local_file(str(entry.get("file") or ""))
        if path is None or entry.get("status") not in (200, None):
            return None
        ext = path.suffix.lower().lstrip(".")
        content_type = entry.get("content_type") or EXT_TYPES.get(ext, "")
        if content_type not in IMAGE_TYPES:
            self.report.note(f"file with an unsupported type skipped ({purpose})")
            return None
        with path.open("rb") as handle:
            head = handle.read(16)
        if not sniff_matches(content_type, head):
            self.report.note(f"file whose bytes do not match its type skipped ({purpose})")
            return None
        key = key_for(IMAGE_TYPES[content_type])
        size = path.stat().st_size
        self.files.append(FileToUpload(path=path, key=key, content_type=content_type, size=size))
        self.tables["files"].append(
            {
                "id": det_uuid("File", key),
                "key": key,
                "owner_id": owner,
                "visibility": visibility,
                "purpose": purpose,
                "content_type": content_type,
                "size_bytes": size,
                "original_name": None,
                "created_at": created,
                "updated_at": created,
            }
        )
        return key

    def file_entry(self, kind: str, ref: str) -> dict[str, Any] | None:
        for entry in self.file_index:
            if entry.get("kind") == kind and str(entry.get("ref")) == ref:
                return entry
        return None

    # --- entities -----------------------------------------------------------------------

    def run(self) -> Bundle:
        self.users()
        self.couriers()
        self.referrals()
        self.places()
        self.shops()
        self.shop_reviews()
        self.orders()
        self.offers()
        self.order_children()
        self.messages()
        self.no_response_cases()
        self.hot_deals()
        self.notifications()
        self.device_tokens()
        self.app_settings()
        self.ledger()
        self.dropped_entities()
        self.qa_counts()
        return Bundle(tables=self.tables, files=self.files, report=self.report, kept=dict(self.kept))

    def users(self) -> None:
        profiles: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for profile in self.export["UserProfile"]:
            profiles[_lower(profile.get("user_id"))].append(profile)
        has_courier = {_lower(c.get("user_id")) for c in self.export["CourierProfile"]}
        seen: set[str] = set()
        for user in sorted(self.export["User"], key=lambda u: u.get("created_date") or ""):
            email = str(user.get("email") or "").strip()
            key = email.lower()
            if not email or "@" not in email:
                self.report.exclude("User", "no e-mail")
                continue
            if key in seen:
                self.report.exclude("User", "duplicate e-mail (case)", mask_email(email))
                continue
            seen.add(key)
            uid = det_uuid("User", user["id"])
            stamps = _stamps(user)
            row: dict[str, Any] = {
                "id": uid,
                "email": email,
                "legacy_b44_id": user["id"],
                "full_name": (user.get("full_name") or "").strip(),
                "email_verified_at": stamps["created_at"] if user.get("is_verified") else None,
                "disabled_at": stamps["updated_at"] if user.get("disabled") else None,
                "role": "admin" if user.get("role") == "admin" else None,
                "language": "ar",
                "phone_e164": None,
                "profile_created_at": None,
                "legacy_profile_b44_id": None,
                "notify_order_status": True,
                "notify_new_orders": True,
                "notify_incoming_orders": True,
                "notify_chat": True,
                "push_enabled": True,
                "whatsapp_opt_in_at": None,
                "is_blacklisted": False,
                "referred_by_code": None,
                "referred_at": None,
                **stamps,
            }
            mine = profiles.pop(key, [])
            if mine:
                self.merge_profiles(row, mine)
            if row["role"] is None:
                row["role"] = "courier" if key in has_courier else "customer"
            self.tables["users"].append(row)
            self.user_by_email[key] = uid
            self.user_by_legacy[user["id"]] = uid
            self.kept["User"].add(user["id"])
        for key, orphans in profiles.items():
            for _ in orphans:
                self.report.exclude("UserProfile", "no Base44 account with this e-mail", mask_email(key))

    def merge_profiles(self, row: dict[str, Any], profiles: list[dict[str, Any]]) -> None:
        """The most recent profile wins; a field it leaves empty is taken from an older one."""
        winner = _latest(profiles)
        others = sorted(
            (p for p in profiles if p is not winner), key=lambda p: p.get("updated_date") or "", reverse=True
        )
        if others:
            self.report.note(
                "duplicate UserProfile merged (most recent kept)", len(others), mask_email(row["email"])
            )

        def pick(name: str) -> Any:
            for profile in (winner, *others):
                value = profile.get(name)
                if not blank(value):
                    if profile is not winner:
                        self.report.note(f"UserProfile.{name} taken from an older duplicate")
                    return value
            return None

        row["legacy_profile_b44_id"] = winner["id"]
        self.kept["UserProfile"].update(p["id"] for p in profiles)
        row["profile_created_at"] = min(_stamps(p)["created_at"] for p in profiles)
        if row["role"] is None and winner.get("role") in ("customer", "courier"):
            row["role"] = winner["role"]
        row["phone_e164"] = self.phone("users", "phone_e164", pick("phone"))
        language = pick("language")
        row["language"] = language if language in ("ar", "fr") else "ar"
        prefs = pick("notification_preferences") or {}
        for legacy, column in (
            ("order_status_changes", "notify_order_status"),
            ("new_orders", "notify_new_orders"),
            ("incoming_orders", "notify_incoming_orders"),
            ("chat_messages", "notify_chat"),
            ("push_notifications_enabled", "push_enabled"),
        ):
            if isinstance(prefs, dict) and isinstance(prefs.get(legacy), bool):
                row[column] = prefs[legacy]
        if winner.get("whatsapp_opt_in"):
            row["whatsapp_opt_in_at"] = (
                parse_dt(winner.get("whatsapp_opt_in_at")) or _stamps(winner)["updated_at"]
            )
        row["is_blacklisted"] = bool(winner.get("is_blacklisted"))
        if winner.get("is_active") is False and row["disabled_at"] is None:
            row["disabled_at"] = _stamps(winner)["updated_at"]
        row["referred_by_code"] = text_or_none(pick("referred_by_code"), 32)
        row["referred_at"] = parse_dt(pick("referred_at"))
        row["_referred_by_courier_legacy"] = pick("referred_by_courier_id")
        self.address(row, winner, pick)

    def address(self, user: dict[str, Any], winner: dict[str, Any], pick: Any) -> None:
        values = {
            "address": text_or_none(pick("default_address"), 500),
            "governorate": text_or_none(pick("governorate"), 120),
            "city": text_or_none(pick("city"), 120),
            "location": point(pick("default_lat"), pick("default_lng")),
        }
        country = text_or_none(pick("country"))
        if country and (len(country) != 2 or not country.isalpha()):
            self.report.adjust("user_addresses", "country", "not a 2-letter code -> NULL")
            country = None
        if not any(values.values()) and not country:
            return
        self.tables["user_addresses"].append(
            {
                "id": det_uuid("UserAddress", winner["id"]),
                "user_id": user["id"],
                "label": None,
                "address": values["address"] or "",
                "details": None,
                "governorate": values["governorate"],
                "city": values["city"],
                "country": country.upper() if country else None,
                "location": values["location"],
                "is_default": True,
                **_stamps(winner),
            }
        )

    def couriers(self) -> None:
        by_user: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for courier in self.export["CourierProfile"]:
            by_user[_lower(courier.get("user_id"))].append(courier)
        referral_codes: set[str] = set()
        for email, rows in by_user.items():
            user_id = self.user_by_email.get(email)
            if user_id is None:
                for _ in rows:
                    self.report.exclude(
                        "CourierProfile", "account deleted in Base44 (no User)", mask_email(email)
                    )
                continue
            keep = _latest(rows)
            cid = det_uuid("CourierProfile", keep["id"])
            for dup in rows:
                if dup is not keep:
                    self.report.exclude(
                        "CourierProfile", "second profile of the same user (references re-pointed)"
                    )
                    self.courier_by_legacy[dup["id"]] = cid
            self.courier_by_legacy[keep["id"]] = cid
            self.courier_user[cid] = user_id
            self.courier_by_user_email[email] = cid
            self.kept["CourierProfile"].add(keep["id"])
            stamps = _stamps(keep)
            row = {
                "id": cid,
                "user_id": user_id,
                "legacy_b44_id": keep["id"],
                "display_name": (keep.get("full_name") or "").strip(),
                "phone_e164": self.phone("couriers", "phone_e164", keep.get("phone")),
                "id_document_number": str(keep.get("cin_passport") or "").strip(),
                "id_document_key": None,
                "vehicle": _enum(keep.get("vehicle_type"), ("walking", "scooter", "car"), "scooter"),
                "max_package": _enum(keep.get("max_package_size"), PACKAGES, "petit"),
                "price_per_km": self.bounded(keep, "price_per_km", 0, 50, cid, default=Decimal(0)),
                "min_fee": self.bounded(keep, "min_fee", 0, 200, cid, default=None),
                "notification_radius_km": self.bounded(
                    keep, "notification_radius_km", 0, 100, cid, default=Decimal(10), places=1
                ),
                "service_governorate": text_or_none(keep.get("service_governorate")),
                "service_city": text_or_none(keep.get("service_city")),
                "service_country": (text_or_none(keep.get("service_country")) or "TN")[:2].upper(),
                "service_start": parse_hhmm(keep.get("service_start_time")),
                "service_end": parse_hhmm(keep.get("service_end_time")),
                "verification": _enum(keep.get("verification_status"), VERIFICATIONS, "pending"),
                "verified_at": None,
                "rejection_reason": None,
                "referral_code": None,
                "late_cancellations": max(0, integer(keep.get("late_cancellations")) or 0),
                # Presence is ephemeral: nobody is online until the app sends a heartbeat again.
                "is_online": False,
                "last_location": point(keep.get("current_lat"), keep.get("current_lng")),
                "last_seen_at": stamps["updated_at"],
                **stamps,
            }  # fmt: skip
            if row["phone_e164"] is None:
                self.audit(
                    AUDIT_ADJUST, "couriers", cid, {"phone": "rejected placeholder"}, {"phone_e164": None}
                )
            code = text_or_none(keep.get("referral_code"), 32)
            if code and code.upper() in referral_codes:
                self.report.adjust("couriers", "referral_code", "duplicate code -> NULL")
            elif code:
                referral_codes.add(code.upper())
                row["referral_code"] = code
            if keep.get("id_photo_uri"):
                entry = self.file_entry("courier_id_photo", keep["id"])
                key = None
                if entry:
                    key = self.add_file(
                        entry,
                        lambda ext, cp=keep["id"], uid=user_id: (
                            f"private/courier_id/{uid}/{det_uuid('File', 'courier_id:' + cp).hex}.{ext}"
                        ),
                        user_id,
                        "private",
                        "courier_id",
                        stamps["created_at"],
                    )
                if key is None:
                    self.report.adjust("couriers", "id_document_key", "ID photo not in the export -> NULL")
                row["id_document_key"] = key
            if keep.get("photo_url"):
                self.report.adjust("couriers", "photo_url", "legacy public ID photo URL dropped")
            self.tables["couriers"].append(row)

    def bounded(
        self, src: dict, name: str, low: int, high: int, cid: Any, default: Any, places: int = 3
    ) -> Decimal | None:
        value = number(src.get(name), places)
        if value is None:
            return default
        if value < low or value > high:
            clamped = Decimal(low if value < low else high)
            self.report.adjust("couriers", name, f"out of [{low}, {high}] -> clamped")
            self.audit(AUDIT_ADJUST, "couriers", cid, {name: value}, {name: clamped})
            return clamped
        return value

    def referrals(self) -> None:
        for row in self.tables["users"]:
            legacy = row.pop("_referred_by_courier_legacy", None)
            if not legacy:
                continue
            courier = self.courier_by_legacy.get(legacy)
            if courier is None or self.courier_user.get(courier) == row["id"]:
                self.report.adjust("users", "referred_by_courier_id", "unknown or own courier -> NULL")
                continue
            self.tables["users_referrals"].append({"id": row["id"], "referred_by_courier_id": courier})

    def places(self) -> None:
        seen: set[str] = set()
        for place in self.export["PlaceIndex"]:
            osm_id = text_or_none(place.get("osm_id"))
            name = text_or_none(place.get("name"))
            location = point(place.get("lat"), place.get("lng"))
            if not osm_id or not name or not location:
                self.report.exclude("PlaceIndex", "no osm_id, name or coordinates")
                continue
            if osm_id in seen:
                self.report.exclude("PlaceIndex", "duplicate osm_id")
                continue
            seen.add(osm_id)
            category = unify_category(place.get("category"))
            if category != str(place.get("category") or "").strip().lower():
                self.report.adjust("places", "category", "English/variant category unified")
            if not text_or_none(place.get("name_norm")):
                self.report.note("PlaceIndex.name_norm was empty (recomputed)")
            stamps = _stamps(place)
            address, city = text_or_none(place.get("address")), text_or_none(place.get("city"))
            self.tables["places"].append(
                {
                    "osm_id": osm_id,
                    "legacy_b44_id": place["id"],
                    "name": name,
                    "name_norm": normalize_text(name),
                    "search_norm": search_text(name, address, city),
                    "category": category or "restaurant",
                    "address": address,
                    "city": city,
                    "governorate": text_or_none(place.get("governorate")),
                    "phone": text_or_none(place.get("phone")),
                    "opening_hours": text_or_none(place.get("opening_hours")),
                    "location": location,
                    "source": text_or_none(place.get("source")) or "osm",
                    "source_ts": parse_dt(place.get("source_ts")),
                    "quality_score": self.quality_percent(place.get("quality_score")),
                    "refreshed_at": stamps["updated_at"],
                    **stamps,
                }
            )
            self.kept["PlaceIndex"].add(place["id"])

    def quality_percent(self, value: Any) -> int | None:
        """Base44 stored 0.5-1.0; our column is a percentage (0-100). A value above 1 is read as
        a percentage already."""
        score = number(value, 4)
        if score is None:
            return None
        if score > 1:
            self.report.adjust("places", "quality_score", "above 1: read as a percentage")
            percent = integer(score)
        else:
            percent = integer(score * 100)
        return max(0, min(100, percent or 0))

    def shops(self) -> None:
        for shop in self.export["Shop"]:
            location = point(shop.get("latitude"), shop.get("longitude"))
            name = text_or_none(shop.get("name"))
            if not location or not name:
                self.report.exclude("Shop", "no name or coordinates")
                continue
            sid = det_uuid("Shop", shop["id"])
            stamps = _stamps(shop)
            status = shop.get("review_status")
            if status not in ("pending", "approved", "rejected"):
                status = "approved"
                self.report.note("Shop.review_status absent -> approved (published before proposals)")
            proposer = self.user_ref(shop.get("proposed_by")) if shop.get("proposed_by") else None
            self.tables["shops"].append(
                {
                    "id": sid,
                    "legacy_b44_id": shop["id"],
                    "_place_osm_id": text_or_none(shop.get("osm_id")),
                    "osm_id": text_or_none(shop.get("osm_id")),
                    "name": name,
                    "address": text_or_none(shop.get("address")),
                    "governorate": text_or_none(shop.get("governorate")),
                    "city": text_or_none(shop.get("city")),
                    "phone": text_or_none(shop.get("phone")),
                    "categories": sorted(
                        {unify_category(c) for c in shop.get("categories") or [] if not blank(c)}
                    ),
                    "opening_hours": text_or_none(shop.get("opening_hours")),
                    "description": text_or_none(shop.get("description")),
                    "photo_key": stored_photo(shop.get("photo_url")),
                    "location": location,
                    "review_status": status,
                    "proposed_by": proposer,
                    "proposed_at": parse_dt(shop.get("proposed_at")),
                    **stamps,
                }
            )
            if shop.get("photo_url"):
                self.report.adjust("shops", "photo_key", "photo not in the export: legacy URL kept")
            self.ids["Shop"][shop["id"]] = sid
            self.kept["Shop"].add(shop["id"])
            for position, item in enumerate(shop.get("menu_items") or []):
                if not isinstance(item, dict) or blank(item.get("name")):
                    self.report.exclude("Shop.menu_items", "item without a name")
                    continue
                price = money(item.get("price"))
                if price is not None and price < 0:
                    price = None
                    self.report.adjust("shop_menu_items", "price", "negative -> NULL")
                photo_key = None
                if item.get("photo_url"):
                    entry = self.file_entry("menu_photo", f"{shop['id']}:{position}")
                    created = stamps["created_at"]
                    if entry:
                        photo_key = self.add_file(
                            entry,
                            lambda ext, ref=f"{shop['id']}:{position}", c=created: (
                                f"public/menu/{c:%Y}/{c:%m}/{det_uuid('File', 'menu:' + ref).hex}.{ext}"
                            ),
                            proposer,
                            "public",
                            "menu",
                            created,
                        )
                    if photo_key is None:
                        photo_key = stored_photo(item.get("photo_url"))
                        self.report.adjust(
                            "shop_menu_items", "photo_key", "not in the export: legacy https URL kept"
                        )
                self.tables["shop_menu_items"].append(
                    {
                        "id": det_uuid("ShopMenuItem", f"{shop['id']}:{position}"),
                        "shop_id": sid,
                        "name": str(item["name"]).strip(),
                        "price": price,
                        "description": text_or_none(item.get("description")),
                        "photo_key": photo_key,
                        "position": position,
                    }
                )

    def shop_reviews(self) -> None:
        """`target_key` = shop_osm_id as the front sent it (catalog rule); `shop:<Base44 id>` becomes
        `shop:<new id>` (the key the front builds from the shop's id); resolved to a shop or a
        place when possible, else kept unresolved like the app does. One review per user and key."""
        shop_by_osm = {r["osm_id"]: r["id"] for r in self.tables["shops"] if r["osm_id"]}
        place_osm = {r["osm_id"] for r in self.tables["places"]}
        seen: set[tuple] = set()
        reviews = sorted(self.export["ShopReview"], key=lambda r: r.get("updated_date") or "", reverse=True)
        for review in reviews:
            user = self.user_by_legacy.get(review.get("user_id")) or self.user_ref(review.get("user_id"))
            rating = integer(review.get("rating"))
            key = text_or_none(review.get("shop_osm_id"))
            if user is None or rating is None or not 1 <= rating <= 5 or not key:
                self.report.exclude("ShopReview", "unknown author, no key or rating outside 1-5")
                continue
            shop_id, place_osm_id = None, None
            if key.startswith("shop:"):
                shop_id = self.ids["Shop"].get(key[5:])
                if shop_id:
                    key = f"shop:{shop_id}"
            elif key in shop_by_osm:
                shop_id = shop_by_osm[key]
            elif key in place_osm:
                place_osm_id = key
            if shop_id is None and place_osm_id is None:
                self.report.note("ShopReview key not resolved to a shop or place (kept, as the app does)")
            marks = {("key", key), ("shop", shop_id), ("place", place_osm_id)}
            marks -= {("shop", None), ("place", None)}
            if any((user, mark) in seen for mark in marks):
                self.report.exclude("ShopReview", "second review of the same user and target (latest kept)")
                continue
            seen.update((user, mark) for mark in marks)
            if review.get("photo_urls"):
                self.report.adjust("shop_reviews", "photo_keys", "photos not in the export -> empty")
            self.tables["shop_reviews"].append(
                {
                    "id": det_uuid("ShopReview", review["id"]),
                    "legacy_b44_id": review["id"],
                    "target_key": key,
                    "shop_id": shop_id,
                    "_place_osm_id": place_osm_id,
                    "place_id": None,
                    "user_id": user,
                    "rating": rating,
                    "comment": text_or_none(review.get("comment")),
                    "photo_keys": [],
                    **_stamps(review),
                }
            )
            self.kept["ShopReview"].add(review["id"])

    # --- orders ---------------------------------------------------------------------------

    def orders(self) -> None:
        accepted_offer: dict[str, dict[str, Any]] = {}
        for offer in self.export["OrderOffer"]:
            if offer.get("status") == "accepted":
                accepted_offer[offer.get("order_id")] = offer
        for order in self.export["Order"]:
            customer = self.user_ref(order.get("customer_id"))
            if customer is None:
                self.report.exclude("Order", "customer account unknown", mask_email(order.get("customer_id")))
                continue
            status = order.get("status")
            if status not in ORDER_STATUSES:
                self.report.exclude("Order", "status outside the enum")
                continue
            items = str(order.get("items_text") or "").strip()
            if not items:
                self.report.exclude("Order", "empty items_text")
                continue
            oid = det_uuid("Order", order["id"])
            stamps = _stamps(order)
            history = self.clean_history(order)
            courier = self.courier_by_legacy.get(order.get("courier_id")) if order.get("courier_id") else None
            if order.get("courier_id") and courier is None:
                self.report.adjust("orders", "courier_id", "unknown CourierProfile -> NULL")
            if courier is None and order.get("courier_user_id"):
                self.report.note(
                    "order keeps a courier e-mail without courier_id (courier left; actor kept on the event)"
                )
            if courier is None and status in COURIER_REQUIRED:
                self.report.exclude("Order", f"status {status} without a courier")
                continue
            row = {
                "id": oid,
                "legacy_b44_id": order["id"],
                "customer_id": customer,
                "courier_id": courier,
                "preferred_courier_id": self.courier_by_legacy.get(order.get("preferred_courier_id")),
                "status": status,
                "items_text": items[:2000],
                "quantity": self.quantity(order),
                "notes": text_or_none(order.get("notes")),
                "alternatives": text_or_none(order.get("alternatives")),
                "package": _enum(order.get("package_size"), PACKAGES, "petit"),
                "estimated_price": self.amount("estimated_price", order, low=0, high=None, oid=oid),
                "contact_name": text_or_none(order.get("customer_name")) or self._full_name(customer),
                "contact_phone_e164": self.phone("orders", "contact_phone_e164", order.get("customer_phone")),
                "delivery_address": str(order.get("delivery_address") or "").strip(),
                "delivery_details": text_or_none(order.get("delivery_details")),
                "delivery_governorate": text_or_none(order.get("delivery_governorate")),
                "delivery_city": text_or_none(order.get("delivery_city")),
                "delivery_location": point(order.get("delivery_lat"), order.get("delivery_lng")),
                "scheduled_for": parse_dt(order.get("scheduled_time")),
                "distance_km": self.bounded_number(order, "distance_km", 2, 9999),
                "eta_minutes": self.bounded_int(order, "eta_minutes", 0, 100000),
                "purchase_amount": self.amount("purchase_amount", order, low=0, high=2000, oid=oid),
                "delivery_fee": self.amount("delivery_fee", order, low=0, high=200, oid=oid),
                "payment_method": "cash",
                "price_confirmed_at": (
                    stamps["updated_at"] if order.get("price_confirmed_by_customer") else None
                ),
                "current_stop_seq": max(0, integer(order.get("current_shop_index")) or 0),
                "cancelled_by": _enum(order.get("cancelled_by"), CANCELLED_BY, None),
                "cancel_reason": text_or_none(order.get("cancellation_reason")),
                "accepted_at": self.status_time(history, "accepted"),
                "delivered_at": None,
                "cancelled_at": None,
                "last_dispatched_at": parse_dt(order.get("last_dispatched_at")),
                **stamps,
            }  # fmt: skip
            if row["accepted_at"] is None and order["id"] in accepted_offer and courier is not None:
                row["accepted_at"] = parse_dt(accepted_offer[order["id"]].get("updated_date"))
                self.report.note("orders.accepted_at from the accepted offer (no history event)")
            if not row["delivery_address"]:
                row["delivery_address"] = "-"
                self.report.adjust("orders", "delivery_address", "empty -> '-'")
            if status == "delivered":
                row["delivered_at"] = self.status_time(history, "delivered") or parse_dt(
                    order.get("courier_stats_recorded_at")
                )
                if row["delivered_at"] is None:
                    row["delivered_at"] = stamps["updated_at"]
                    self.report.adjust("orders", "delivered_at", "no delivered event -> updated_date")
            if status == "cancelled":
                row["cancelled_at"] = parse_dt(order.get("cancelled_at")) or self.status_time(
                    history, "cancelled"
                )
                if row["cancelled_at"] is None:
                    row["cancelled_at"] = stamps["updated_at"]
                    self.report.adjust("orders", "cancelled_at", "no date nor event -> updated_date")
            elif order.get("cancelled_at"):
                row["cancelled_at"] = parse_dt(order.get("cancelled_at"))
            if order.get("cancelled_by") and row["cancelled_by"] is None:
                self.report.adjust("orders", "cancelled_by", "value outside the enum -> NULL")
            self.tables["orders"].append(row)
            self.order_by_legacy[order["id"]] = oid
            self.order_rows[order["id"]] = row
            self.kept["Order"].add(order["id"])
            self.events(order, row, history)
            self.stops(order, row)

    def _full_name(self, user_id: Any) -> str:
        for user in self.tables["users"]:
            if user["id"] == user_id:
                return user["full_name"]
        return ""

    def quantity(self, order: dict[str, Any]) -> int:
        qty = integer(order.get("quantity")) or 1
        if not 1 <= qty <= 100:
            self.report.adjust("orders", "quantity", "outside 1-100 -> clamped")
            qty = min(100, max(1, qty))
        return qty

    def amount(self, name: str, order: dict, low: int, high: int | None, oid: Any) -> Decimal | None:
        raw = order.get(name)
        value = money(raw)
        if value is None:
            return None
        if value != Decimal(str(raw)):
            self.report.adjust("orders", name, "rounded to the millime")
        if value < low or (high is not None and value > high):
            self.report.adjust("orders", name, f"outside [{low}, {high}] -> NULL (original in audit_log)")
            self.audit(AUDIT_ADJUST, "orders", oid, {name: value}, {name: None})
            return None
        return value

    def bounded_number(self, row: dict, name: str, places: int, high: int) -> Decimal | None:
        value = number(row.get(name), places)
        if value is not None and not 0 <= value <= high:
            self.report.adjust("orders", name, "out of range -> NULL")
            return None
        return value

    def bounded_int(self, row: dict, name: str, low: int, high: int) -> int | None:
        value = integer(row.get(name))
        if value is not None and not low <= value <= high:
            self.report.adjust("orders", name, "out of range -> NULL")
            return None
        return value

    def clean_history(self, order: dict[str, Any]) -> list[dict[str, Any]]:
        out = []
        for item in order.get("status_history") or []:
            if not isinstance(item, dict) or item.get("status") not in ORDER_STATUSES:
                self.report.adjust(
                    "order_status_events", "to_status", "history item outside the enum dropped"
                )
                continue
            at = parse_dt(item.get("timestamp"))
            if at is None:
                self.report.adjust(
                    "order_status_events", "created_at", "history item without timestamp dropped"
                )
                continue
            out.append({**item, "_at": at})
        out.sort(key=lambda i: i["_at"])
        return out

    @staticmethod
    def status_time(history: list[dict[str, Any]], status: str) -> datetime | None:
        for item in reversed(history):
            if item["status"] == status:
                return item["_at"]
        return None

    def actor(self, order: dict[str, Any], row: dict[str, Any], cancelled_by: str | None) -> Any:
        if cancelled_by == "customer":
            return row["customer_id"]
        if cancelled_by == "courier":
            if row["courier_id"]:
                return self.courier_user.get(row["courier_id"])
            return self.user_ref(order.get("courier_user_id")) if order.get("courier_user_id") else None
        return None

    def events(self, order: dict[str, Any], row: dict[str, Any], history: list[dict[str, Any]]) -> None:
        events: list[dict[str, Any]] = []
        previous: str | None = None
        if not history:
            self.report.note("order without status_history (pending + final events synthesized)")
            events.append(self.event(row, None, "pending", row["created_at"], MIGRATION_SOURCE))
            previous = "pending"
        for item in history:
            cancelled_by = item.get("cancelled_by") if item.get("cancelled_by") in CANCELLED_BY else None
            events.append(
                {
                    **self.event(row, previous, item["status"], item["_at"], item.get("source") or "legacy"),
                    "reason": text_or_none(item.get("reason")),
                    "cancelled_by": cancelled_by,
                    "location": point(item.get("lat"), item.get("lng")),
                    "actor_user_id": self.actor(order, row, cancelled_by),
                }
            )
            previous = item["status"]
        if previous != row["status"]:
            if history:
                self.report.note("status_history last != status (synthetic event added)")
            at = row["delivered_at"] or row["cancelled_at"] or row["updated_at"]
            at = max(at, events[-1]["created_at"]) if events else at
            events.append(
                {
                    **self.event(row, previous, row["status"], at, MIGRATION_SOURCE),
                    "reason": row["cancel_reason"] if row["status"] == "cancelled" else None,
                    "cancelled_by": row["cancelled_by"] if row["status"] == "cancelled" else None,
                    "actor_user_id": self.actor(order, row, row["cancelled_by"])
                    if row["status"] == "cancelled"
                    else None,
                }
            )
        self.tables["order_status_events"].extend(events)

    @staticmethod
    def event(row: dict[str, Any], previous: str | None, status: str, at: datetime, source: str) -> dict:
        return {
            "order_id": row["id"],
            "from_status": previous,
            "to_status": status,
            "actor_user_id": None,
            "source": source,
            "reason": None,
            "cancelled_by": None,
            "location": None,
            "created_at": at,
        }

    def stops(self, order: dict[str, Any], row: dict[str, Any]) -> None:
        shops = [s for s in (order.get("shops") or []) if isinstance(s, dict)]
        first_extra = {
            "phone": text_or_none(order.get("shop_phone")),
            "governorate": text_or_none(order.get("shop_governorate")),
            "city": text_or_none(order.get("shop_city")),
        }
        if not shops:
            if blank(order.get("shop_name")):
                return
            self.report.note("order without shops[]: stop 0 built from shop_* fields")
            shops = [
                {
                    "name": order.get("shop_name"),
                    "address": order.get("shop_address"),
                    "lat": order.get("shop_lat"),
                    "lng": order.get("shop_lng"),
                    "status": "pending",
                }
            ]
        for seq, shop in enumerate(shops):
            status = shop.get("status") if shop.get("status") in STOP_STATUSES else "pending"
            if shop.get("status") and shop.get("status") not in STOP_STATUSES:
                self.report.adjust("order_stops", "status", "outside the enum -> pending")
            amount = money(shop.get("purchase_amount"))
            if amount is not None and not 0 <= amount <= 2000:
                self.report.adjust("order_stops", "purchase_amount", "out of range -> NULL")
                amount = None
            if shop.get("receipt_photo_url"):
                self.report.adjust("order_stops", "receipt_key", "receipt photo not in the export -> NULL")
            extra = first_extra if seq == 0 else {"phone": None, "governorate": None, "city": None}
            self.tables["order_stops"].append(
                {
                    "id": det_uuid("OrderStop", f"{order['id']}:{seq}"),
                    "order_id": row["id"],
                    "seq": seq,
                    "shop_id": None,
                    "place_id": None,
                    "name": text_or_none(shop.get("name")) or text_or_none(order.get("shop_name")) or "-",
                    "address": text_or_none(shop.get("address")),
                    **extra,
                    "location": point(shop.get("lat"), shop.get("lng")),
                    "items": text_or_none(shop.get("items")),
                    "status": status,
                    "purchase_amount": amount,
                    "receipt_key": None,
                    "completed_at": parse_dt(shop.get("completed_at")),
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
            )

    def offers(self) -> None:
        kept: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for offer in self.export["OrderOffer"]:
            order = self.order_by_legacy.get(offer.get("order_id"))
            courier = self.courier_by_legacy.get(offer.get("courier_id"))
            if order is None:
                self.report.exclude("OrderOffer", "order deleted in Base44 (dangling order_id)")
                continue
            if courier is None:
                self.report.exclude("OrderOffer", "unknown courier")
                continue
            oid = det_uuid("OrderOffer", offer["id"])
            fee = money(offer.get("proposed_fee"))
            if fee is None or not Decimal(0) < fee <= 200:
                self.report.exclude("OrderOffer", "proposed_fee outside ]0, 200] (row in audit_log)")
                self.audit(
                    AUDIT_EXCLUDE,
                    "order_offers",
                    offer["id"],
                    {
                        "order_id": str(order),
                        "courier_id": str(courier),
                        "proposed_fee": fee,
                        "status": offer.get("status"),
                        "created_date": offer.get("created_date"),
                    },
                    None,
                )
                continue
            eta = integer(offer.get("eta_minutes"))
            if eta is not None and not 0 <= eta <= 600:
                self.report.adjust(
                    "order_offers", "eta_minutes", "outside 0-600 -> NULL (original in audit_log)"
                )
                self.audit(AUDIT_ADJUST, "order_offers", oid, {"eta_minutes": eta}, {"eta_minutes": None})
                eta = None
            status = offer.get("status") if offer.get("status") in OFFER_STATUSES else "expired"
            stamps = _stamps(offer)
            rating = number(offer.get("courier_rating"), 2)
            row = {
                "id": oid,
                "legacy_b44_id": offer["id"],
                "order_id": order,
                "courier_id": courier,
                "proposed_fee": fee,
                "eta_minutes": eta,
                "distance_km": number(offer.get("distance_km"), 2),
                "message": text_or_none(offer.get("message"), 500),
                "courier_rating_snapshot": rating if rating is not None and 0 <= rating <= 5 else None,
                "status": status,
                "decided_at": stamps["updated_at"] if status in ("accepted", "rejected", "expired") else None,
                **stamps,
            }
            kept.append((offer, row))
        self.resolve_offer_duplicates(kept)
        for offer, row in kept:
            self.tables["order_offers"].append(row)
            self.ids["OrderOffer"][offer["id"]] = row["id"]
            self.kept["OrderOffer"].add(offer["id"])

    def resolve_offer_duplicates(self, kept: list[tuple[dict[str, Any], dict[str, Any]]]) -> None:
        """At most one pending offer per (order, courier) and one accepted offer per order."""
        pending: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
        accepted: dict[Any, list[dict[str, Any]]] = defaultdict(list)
        for _, row in kept:
            if row["status"] == "pending":
                pending[(row["order_id"], row["courier_id"])].append(row)
            elif row["status"] == "accepted":
                accepted[row["order_id"]].append(row)
        for rows in pending.values():
            rows.sort(key=lambda r: r["created_at"])
            for row in rows[:-1]:
                row["status"], row["decided_at"] = "expired", row["updated_at"]
                self.report.adjust("order_offers", "status", "older duplicate pending offer -> expired")
        orders = {r["id"]: r for r in self.tables["orders"]}
        for order_id, rows in accepted.items():
            if len(rows) < 2:
                continue
            courier = orders[order_id]["courier_id"]
            rows.sort(key=lambda r: (r["courier_id"] == courier, r["updated_at"]))
            for row in rows[:-1]:
                row["status"] = "rejected"
                self.report.adjust("order_offers", "status", "second accepted offer of an order -> rejected")

    def order_children(self) -> None:
        for order in self.export["Order"]:
            row = self.order_rows.get(order["id"])
            if row is None:
                continue
            self.rating(order, row)
            self.tracking(order, row)
            self.issues(order, row)

    def rating(self, order: dict[str, Any], row: dict[str, Any]) -> None:
        rating = integer(order.get("customer_rating"))
        if rating is None:
            return
        if not 1 <= rating <= 5 or row["courier_id"] is None:
            self.report.exclude("Order.customer_rating", "outside 1-5 or order without courier")
            return
        self.tables["order_ratings"].append(
            {
                "order_id": row["id"],
                "courier_id": row["courier_id"],
                "rater_id": row["customer_id"],
                "rating": rating,
                "comment": text_or_none(order.get("rating_comment")),
                "created_at": row["delivered_at"] or row["updated_at"],
            }
        )

    def tracking(self, order: dict[str, Any], row: dict[str, Any]) -> None:
        location = point(order.get("courier_live_lat"), order.get("courier_live_lng"))
        if location is None:
            return
        if row["courier_id"] is None:
            self.report.exclude("Order.courier_live_*", "position without an assigned courier")
            return
        self.tables["order_tracking"].append(
            {
                "order_id": row["id"],
                "courier_id": row["courier_id"],
                "location": location,
                "recorded_at": parse_dt(order.get("courier_live_at")) or row["updated_at"],
            }
        )

    def issues(self, order: dict[str, Any], row: dict[str, Any]) -> None:
        for index, issue in enumerate(order.get("reported_issues") or []):
            if not isinstance(issue, dict):
                continue
            if issue.get("photo_url"):
                self.report.adjust("order_issues", "photo_key", "photo not in the export -> NULL")
            self.tables["order_issues"].append(
                {
                    "id": det_uuid("OrderIssue", f"{order['id']}:{index}"),
                    "order_id": row["id"],
                    "reporter_id": self.user_ref(issue.get("reported_by"))
                    if issue.get("reported_by")
                    else None,
                    "issue_type": text_or_none(issue.get("type")) or "other",
                    "description": text_or_none(issue.get("description")),
                    "photo_key": None,
                    "resolved_at": None,
                    "created_at": parse_dt(issue.get("reported_at")) or row["updated_at"],
                }
            )

    # --- messaging ------------------------------------------------------------------------

    def order_courier_user(self, legacy_order_id: str) -> Any:
        row = self.order_rows.get(legacy_order_id)
        if row is None:
            return None
        if row["courier_id"]:
            return self.courier_user.get(row["courier_id"])
        source = next((o for o in self.export["Order"] if o["id"] == legacy_order_id), {})
        return self.user_ref(source.get("courier_user_id")) if source.get("courier_user_id") else None

    def messages(self) -> None:
        for message in self.export["Message"]:
            order = self.order_by_legacy.get(message.get("order_id"))
            if order is None:
                self.report.exclude("Message", "order deleted in Base44 (dangling order_id)")
                continue
            body = str(message.get("content") or "").strip()
            role = message.get("sender_role")
            if not body or role not in ("customer", "courier"):
                self.report.exclude("Message", "empty body or unknown sender_role")
                continue
            if len(body) > 1000:
                body = body[:1000]
                self.report.adjust("messages", "body", "longer than 1000 -> truncated")
            if is_b44_id(message.get("sender_id")) and message.get("sender_id") in self.courier_by_legacy:
                self.report.adjust("messages", "sender_id", "CourierProfile id -> the courier's user")
            sender = self.user_ref(message.get("sender_id"))
            if sender is None:
                self.report.adjust("messages", "sender_id", "unknown sender -> NULL")
            recipient = self.user_ref(message.get("recipient_id")) if message.get("recipient_id") else None
            stamps = _stamps(message)
            if recipient is None:
                # Old rows have no recipient. A courier writes to the customer; a customer writes
                # to the courier only once one accepted — before that the order is open and NULL
                # means "no single recipient" (messaging rule).
                order_row = self.order_rows[message["order_id"]]
                accepted = order_row["accepted_at"]
                if role == "courier":
                    recipient = order_row["customer_id"]
                elif accepted is not None and stamps["created_at"] >= accepted:
                    recipient = self.order_courier_user(message["order_id"])
                if recipient is not None:
                    self.report.adjust("messages", "recipient_id", "absent -> inferred from the order")
                else:
                    self.report.note("message without recipient (customer message on an open order)")
            mid = det_uuid("Message", message["id"])
            self.tables["messages"].append(
                {
                    "id": mid,
                    "legacy_b44_id": message["id"],
                    "order_id": order,
                    "sender_id": sender,
                    "recipient_id": recipient,
                    "sender_role": role,
                    "body": body,
                    "is_template": bool(message.get("is_template")),
                    "read_at": stamps["updated_at"] if message.get("is_read") else None,
                    **stamps,
                }
            )
            self.ids["Message"][message["id"]] = mid
            self.kept["Message"].add(message["id"])

    def no_response_cases(self) -> None:
        channels_by_case = {}
        for order in self.export["Order"]:
            if order.get("no_response_case_id") and isinstance(order.get("no_response_channels"), dict):
                channels_by_case[order["no_response_case_id"]] = order["no_response_channels"]
        rows = []
        for case in self.export["NoResponseCase"]:
            order = self.order_rows.get(case.get("order_id"))
            if order is None:
                self.report.exclude("NoResponseCase", "order deleted in Base44 (dangling order_id)")
                continue
            stamps = _stamps(case)
            started = parse_dt(case.get("started_at")) or stamps["created_at"]
            deadline = parse_dt(case.get("deadline_at"))
            if deadline is None:
                deadline = started
                self.report.adjust("no_response_cases", "deadline_at", "missing -> started_at")
            status = (
                case.get("status") if case.get("status") in ("waiting", "expired", "resolved") else "expired"
            )
            resolution = case.get("resolution")
            if resolution is not None and resolution not in NO_RESPONSE_RESOLUTIONS:
                self.report.adjust("no_response_cases", "resolution", "outside the list -> NULL")
                resolution = None
            cid = det_uuid("NoResponseCase", case["id"])
            resolved_at = parse_dt(case.get("resolved_at"))
            if status == "waiting" and order["status"] in ("delivered", "cancelled"):
                before = {"status": status, "resolution": resolution, "resolved_at": resolved_at}
                status = "resolved"
                resolution = "delivered" if order["status"] == "delivered" else "order_cancelled"
                resolved_at = order["delivered_at"] or order["cancelled_at"] or order["updated_at"]
                self.report.adjust(
                    "no_response_cases",
                    "status",
                    "waiting on a closed order -> resolved (sweep would act on it)",
                )
                self.audit(
                    AUDIT_ADJUST, "no_response_cases", cid, before,
                    {"status": status, "resolution": resolution, "resolved_at": resolved_at},
                )  # fmt: skip
            channels = dict(channels_by_case.get(case["id"], {}))
            if case.get("push_devices") is not None:
                channels["push_devices"] = integer(case.get("push_devices"))
            row = {
                "id": cid,
                "legacy_b44_id": case["id"],
                "order_id": order["id"],
                "courier_id": self.courier_by_legacy.get(case.get("courier_id")),
                "status": status,
                "purchase_amount": money(case.get("purchase_amount")),
                "started_at": started,
                "deadline_at": deadline,
                "final_at": parse_dt(case.get("final_at")),
                "resolved_at": resolved_at,
                "resolution": resolution,
                "incident_counted": bool(case.get("incident_counted")),
                "customer_answered_late": bool(case.get("customer_answered_late")),
                "channels": channels,
                "messaging_status": text_or_none(case.get("messaging_status")),
                **stamps,
            }
            rows.append(row)
            self.ids["NoResponseCase"][case["id"]] = cid
            self.kept["NoResponseCase"].add(case["id"])
        waiting: dict[Any, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            if row["status"] == "waiting":
                waiting[row["order_id"]].append(row)
        for group in waiting.values():
            group.sort(key=lambda r: r["started_at"])
            for row in group[:-1]:
                row["status"] = "expired"
                self.report.adjust(
                    "no_response_cases", "status", "second waiting case of an order -> expired"
                )
        self.tables["no_response_cases"].extend(rows)

    def hot_deals(self) -> None:
        buyer_orders: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for order in self.export["Order"]:
            if order.get("resale_order_id") and order["id"] in self.order_rows:
                buyer_orders[order["resale_order_id"]].append(self.order_rows[order["id"]])
        for deal in self.export["ResaleOrder"]:
            original = self.order_by_legacy.get(deal.get("original_order_id"))
            courier = self.courier_by_legacy.get(deal.get("courier_id"))
            purchase = money(deal.get("purchase_amount"))
            if original is None:
                self.report.exclude("ResaleOrder", "original order deleted in Base44 / test id (dangling)")
                continue
            if courier is None or purchase is None or purchase < 0 or blank(deal.get("items_text")):
                self.report.exclude("ResaleOrder", "unknown courier, amount or items")
                continue
            stamps = _stamps(deal)
            status = (
                deal.get("status") if deal.get("status") in ("available", "sold", "expired") else "expired"
            )
            buyer = self.user_ref(deal.get("buyer_id")) if deal.get("buyer_id") else None
            if status == "sold" and buyer is None:
                self.report.adjust("hot_deals", "status", "sold without a known buyer -> expired")
                status = "expired"
            discount = number(deal.get("discount_percentage"), 2) or Decimal(0)
            price = money(deal.get("discounted_price"))
            if price is None or price < 0:
                price = purchase
                self.report.adjust("hot_deals", "price", "missing -> purchase_amount")
            candidates = sorted(
                buyer_orders.get(deal["id"], []), key=lambda r: (r["customer_id"] == buyer, r["created_at"])
            )
            deal_id = det_uuid("ResaleOrder", deal["id"])
            if deal.get("photo_url"):
                self.report.adjust("hot_deals", "photo_key", "photo not in the export -> NULL")
            self.tables["hot_deals"].append(
                {
                    "id": deal_id,
                    "legacy_b44_id": deal["id"],
                    "original_order_id": original,
                    "courier_id": courier,
                    "items_text": str(deal["items_text"]).strip(),
                    "shop_name": text_or_none(deal.get("shop_name")),
                    "shop_address": text_or_none(deal.get("shop_address")),
                    "purchase_amount": purchase,
                    "discount_percentage": min(Decimal(100), max(Decimal(0), discount)),
                    "price": price,
                    "include_delivery": deal.get("include_delivery") is not False,
                    "delivery_fee": money(deal.get("delivery_fee")),
                    "photo_key": None,
                    "pickup_location": point(deal.get("courier_lat"), deal.get("courier_lng")),
                    "status": status,
                    "expires_at": parse_dt(deal.get("expires_at")) or stamps["created_at"],
                    "buyer_id": buyer,
                    "reserved_at": stamps["updated_at"] if status == "sold" else None,
                    "buyer_order_id": candidates[-1]["id"] if candidates else None,
                    **stamps,
                }
            )
            self.ids["ResaleOrder"][deal["id"]] = deal_id
            self.kept["ResaleOrder"].add(deal["id"])
        for legacy, rows in buyer_orders.items():
            deal_id = self.ids["ResaleOrder"].get(legacy)
            for row in rows:
                if deal_id is None:
                    self.report.adjust("orders", "resale_deal_id", "hot deal not migrated -> NULL")
                    continue
                self.tables["orders_resale_links"].append({"id": row["id"], "resale_deal_id": deal_id})

    def notifications(self) -> None:
        maps = {
            "Order": self.order_by_legacy,
            "OrderOffer": self.ids["OrderOffer"],
            "CourierProfile": self.courier_by_legacy,
            "NoResponseCase": self.ids["NoResponseCase"],
            "ResaleOrder": self.ids["ResaleOrder"],
            "Message": self.ids["Message"],
        }
        for notification in self.export["Notification"]:
            if (
                is_b44_id(notification.get("user_id"))
                and notification.get("user_id") in self.courier_by_legacy
            ):
                self.report.adjust("notifications", "user_id", "CourierProfile id -> the courier's user")
            user = self.user_ref(notification.get("user_id"))
            if user is None:
                self.report.exclude("Notification", "recipient account deleted / unknown")
                continue
            kind = NOTIFICATION_TYPE_SYNONYMS.get(notification.get("type"), notification.get("type"))
            if kind != notification.get("type"):
                self.report.adjust("notifications", "type", f"synonym {notification.get('type')} -> {kind}")
            if kind not in NOTIFICATION_TYPES:
                self.report.exclude("Notification", "type outside the list")
                continue
            order = self.order_by_legacy.get(notification.get("order_id"))
            if notification.get("order_id") and order is None:
                self.report.adjust("notifications", "order_id", "order deleted in Base44 / test id -> NULL")
            data = (
                dict(notification.get("metadata") or {})
                if isinstance(notification.get("metadata"), dict)
                else {}
            )
            for key, entity in METADATA_ID_KEYS.items():
                value = data.get(key)
                if is_b44_id(value):
                    new = maps[entity].get(value)
                    if new is not None:
                        data[key] = str(new)
                    else:
                        self.report.note(f"notification metadata.{key}: legacy id kept (target not migrated)")
            stamps = _stamps(notification)
            self.tables["notifications"].append(
                {
                    "id": det_uuid("Notification", notification["id"]),
                    "legacy_b44_id": notification["id"],
                    "user_id": user,
                    "order_id": order,
                    "type": kind,
                    "title_ar": text_or_none(notification.get("title_ar")),
                    "title_fr": text_or_none(notification.get("title_fr")),
                    "body_ar": text_or_none(notification.get("body_ar")),
                    "body_fr": text_or_none(notification.get("body_fr")),
                    "data": _jsonable(data),
                    "read_at": stamps["updated_at"] if notification.get("is_read") else None,
                    **stamps,
                }
            )
            self.kept["Notification"].add(notification["id"])

    def device_tokens(self) -> None:
        by_token: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for token in self.export["DeviceToken"]:
            if blank(token.get("token")):
                self.report.exclude("DeviceToken", "empty token")
                continue
            by_token[str(token["token"]).strip()].append(token)
        for value, rows in by_token.items():
            keep = max(rows, key=lambda r: (r.get("last_seen_at") or "", r.get("updated_date") or ""))
            for _ in range(len(rows) - 1):
                self.report.exclude("DeviceToken", "same token registered twice (latest kept)")
            user = self.user_ref(keep.get("user_id"))
            if user is None:
                self.report.exclude("DeviceToken", "unknown account")
                continue
            platform = keep.get("platform") if keep.get("platform") in ("web", "android", "ios") else "web"
            stamps = _stamps(keep)
            self.tables["device_tokens"].append(
                {
                    "id": det_uuid("DeviceToken", keep["id"]),
                    "legacy_b44_id": keep["id"],
                    "user_id": user,
                    "token": value,
                    "platform": platform,
                    "app_version": text_or_none(keep.get("app_version")),
                    "device_model": text_or_none(keep.get("device_model")),
                    "locale": text_or_none(keep.get("locale")),
                    "failure_count": max(0, integer(keep.get("failure_count")) or 0),
                    "last_error": text_or_none(keep.get("last_error")),
                    "is_active": keep.get("is_active") is not False,
                    "last_seen_at": parse_dt(keep.get("last_seen_at")) or stamps["updated_at"],
                    **stamps,
                }
            )
            self.kept["DeviceToken"].add(keep["id"])

    def app_settings(self) -> None:
        builtin = {
            "id",
            "key",
            "created_date",
            "updated_date",
            "created_by",
            "created_by_id",
            "is_sample",
            "updated_by",
        }
        for setting in self.export["AppSettings"]:
            key = text_or_none(setting.get("key"))
            if not key:
                self.report.exclude("AppSettings", "no key")
                continue
            self.tables["app_settings"].append(
                {
                    "key": key,
                    "value": _jsonable({k: v for k, v in setting.items() if k not in builtin}),
                    "updated_by": self.user_ref(setting.get("updated_by"))
                    if setting.get("updated_by")
                    else None,
                    **_stamps(setting),
                }
            )
            self.kept["AppSettings"].add(setting.get("id") or key)

    def ledger(self) -> None:
        for row in self.tables["orders"]:
            if row["status"] != "delivered" or row["courier_id"] is None:
                continue
            if not (row["delivery_fee"] or 0) > 0:
                continue
            if row["delivered_at"] >= LAUNCH_END:
                self.report.note("delivered after the launch end: no ledger entry generated (run statements)")
                continue
            self.tables["courier_ledger_entries"].append(
                {
                    "courier_id": row["courier_id"],
                    "order_id": row["id"],
                    "kind": "commission_waived_launch",
                    "amount": COMMISSION_PER_DELIVERY,
                    "created_by": None,
                    "created_at": row["delivered_at"],
                }
            )

    def dropped_entities(self) -> None:
        for entity, reason in (
            ("DeliveryTariffs", "dead table, not migrated (FIELD_MAPPING.md)"),
            ("MessageLog", "WhatsApp/SMS log, not migrated (never configured in Base44)"),
        ):
            for _ in self.export.get(entity, []):
                self.report.exclude(entity, reason)

    def qa_counts(self) -> None:
        if not self.qa_emails:
            return
        qa_users = {self.user_by_email[e] for e in self.qa_emails if e in self.user_by_email}
        self.report.note("QA accounts found in the export", len(qa_users))
        self.report.note(
            "orders placed by QA accounts", sum(r["customer_id"] in qa_users for r in self.tables["orders"])
        )
        qa_couriers = {c for c, u in self.courier_user.items() if u in qa_users}
        self.report.note(
            "orders delivered/handled by QA couriers",
            sum(r["courier_id"] in qa_couriers for r in self.tables["orders"]),
        )
        self.report.note(
            "notifications to QA accounts",
            sum(r["user_id"] in qa_users for r in self.tables["notifications"]),
        )


def _enum(value: Any, allowed: Any, default: Any) -> Any:
    return value if value in allowed else default


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "hex") and hasattr(value, "version"):  # uuid
        return str(value)
    return value


def qa_emails_from_constants(path: Path | None) -> set[str]:
    """The Playwright QA accounts (ods-delivery tests/helpers/constants.ts), same parsing as the seed."""
    if path is None or not path.is_file():
        return set()
    pattern = re.compile(r"export const (\w+_EMAIL)\s*=\s*process\.env\.\w+\s*\?\?\s*'([^']*)'")
    found = {value for _, value in pattern.findall(path.read_text(encoding="utf-8"))}
    return {e for e in found if e} | {
        v for k, v in os.environ.items() if k.startswith("TEST_") and k.endswith("_EMAIL")
    }


def transform(
    export_dir: Path, *, fallback_regions: tuple[str, ...] = ("CH",), qa_emails: set[str] | None = None
) -> Bundle:
    export = load_export(export_dir)
    return Transformer(
        export,
        export_dir=export_dir,
        file_index=load_file_index(export_dir),
        fallback_regions=fallback_regions,
        qa_emails=qa_emails,
    ).run()


def dump(bundle: Bundle, out_dir: Path) -> None:
    """Writes the intermediate as JSON (mode 600) — only ever outside the repository."""
    repo = Path(__file__).resolve().parents[1]
    if out_dir.resolve().is_relative_to(repo):
        raise SystemExit("refusing to write migrated data inside the repository")
    out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    for table, rows in bundle.tables.items():
        path = out_dir / f"{table}.json"
        path.write_text(json.dumps(_jsonable(rows), ensure_ascii=False, default=str))
        path.chmod(0o600)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("export_dir", type=Path)
    parser.add_argument("--fallback-region", action="append", default=None, help="default: CH")
    parser.add_argument("--qa-constants", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None, help="dump the intermediate (outside the repo)")
    args = parser.parse_args(argv)
    bundle = transform(
        args.export_dir,
        fallback_regions=tuple(args.fallback_region or ["CH"]),
        qa_emails=qa_emails_from_constants(args.qa_constants),
    )
    for table in TABLE_ORDER:
        print(f"{table:24} {len(bundle.rows(table)):6}")
    for (entity, reason), count in sorted(bundle.report.excluded.items()):
        print(f"excluded {entity}: {reason}: {count}")
    if args.out:
        dump(bundle, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
