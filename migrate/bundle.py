"""The normalized intermediate produced by transform.py and consumed by import.py / verify.py."""

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Import order: every table after the tables it references. Two pseudo-steps close the
# circular references (users.referred_by_courier_id, orders.resale_deal_id).
TABLE_ORDER = (
    "users",
    "user_addresses",
    "couriers",
    "users_referrals",
    "files",
    "places",
    "shops",
    "shop_menu_items",
    "shop_reviews",
    "orders",
    "order_stops",
    "order_status_events",
    "order_offers",
    "order_tracking",
    "order_ratings",
    "order_issues",
    "messages",
    "notifications",
    "device_tokens",
    "no_response_cases",
    "hot_deals",
    "orders_resale_links",
    "app_settings",
    "courier_ledger_entries",
    "audit_log",
)


@dataclass(frozen=True)
class FileToUpload:
    path: Path
    key: str
    content_type: str
    size: int


@dataclass
class Report:
    """Aggregates only: counts and masked examples (never personal data in clear)."""

    export_counts: dict[str, int] = field(default_factory=dict)
    excluded: Counter = field(default_factory=Counter)  # (entity, reason) -> rows
    adjusted: Counter = field(default_factory=Counter)  # (table, field, what) -> rows
    notes: Counter = field(default_factory=Counter)  # free-form anomaly counters
    examples: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))

    def exclude(self, entity: str, reason: str, example: str | None = None) -> None:
        self.excluded[(entity, reason)] += 1
        self._example(f"excluded {entity}: {reason}", example)

    def adjust(self, table: str, column: str, what: str, example: str | None = None) -> None:
        self.adjusted[(table, column, what)] += 1
        self._example(f"adjusted {table}.{column}: {what}", example)

    def note(self, key: str, count: int = 1, example: str | None = None) -> None:
        self.notes[key] += count
        self._example(key, example)

    def _example(self, key: str, example: str | None) -> None:
        if example is not None and len(self.examples[key]) < 3:
            self.examples[key].append(example)

    def excluded_count(self, entity: str) -> int:
        return sum(n for (ent, _), n in self.excluded.items() if ent == entity)


@dataclass
class Bundle:
    tables: dict[str, list[dict[str, Any]]]
    files: list[FileToUpload]
    report: Report
    # entity -> legacy ids that made it into the target (verify compares against these)
    kept: dict[str, set[str]] = field(default_factory=dict)

    def rows(self, table: str) -> list[dict[str, Any]]:
        return self.tables.get(table, [])
