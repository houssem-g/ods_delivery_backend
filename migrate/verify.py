"""Checks an imported database against the Base44 export it came from.

- counts per table: every row kept by the transform is in the database (legacy ids), and
  export rows = kept + documented exclusions;
- counts per status (orders, offers, cases, deals) and per notification type;
- money: delivery fees, purchase amounts, offer fees, hot-deal prices, ledger — recomputed
  from the raw export with the documented rules, not read back from the transform;
- integrity: status/courier/timestamp rules, event history ends on the order status, stops;
- samples: N random orders compared field by field with the export;
- derived counters (courier_stats) against the export's delivered orders and ratings.

Reports are aggregate / masked: legacy ids at most, never names, e-mails or phones.

    uv run python -m migrate.verify EXPORT_DIR --database-url URL [--samples 20] [--seed 1] [--check-files]
"""

import argparse
import asyncio
import random
import sys
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.models.notifications import NOTIFICATION_TYPE_SYNONYMS
from migrate.bundle import Bundle
from migrate.common import money, normalize_phone, parse_dt
from migrate.importer import make_engine
from migrate.transform import load_export, qa_emails_from_constants, transform

# entity -> (table, legacy column)
LEGACY_TABLES = {
    "User": ("users", "legacy_b44_id"),
    "CourierProfile": ("couriers", "legacy_b44_id"),
    "Order": ("orders", "legacy_b44_id"),
    "OrderOffer": ("order_offers", "legacy_b44_id"),
    "Message": ("messages", "legacy_b44_id"),
    "Notification": ("notifications", "legacy_b44_id"),
    "DeviceToken": ("device_tokens", "legacy_b44_id"),
    "ResaleOrder": ("hot_deals", "legacy_b44_id"),
    "NoResponseCase": ("no_response_cases", "legacy_b44_id"),
    "Shop": ("shops", "legacy_b44_id"),
    "ShopReview": ("shop_reviews", "legacy_b44_id"),
    "PlaceIndex": ("places", "legacy_b44_id"),
}
# rows of these tables hang off migrated orders / users: compared with the transform's output
ORDER_CHILDREN = ("order_stops", "order_ratings", "order_tracking", "order_issues", "courier_ledger_entries")
DERIVED = {
    **{
        name: f"SELECT count(*) FROM {name} x JOIN orders o ON o.id = x.order_id "
        "WHERE o.legacy_b44_id IS NOT NULL"
        for name in ORDER_CHILDREN
    },
    "user_addresses": "SELECT count(*) FROM user_addresses a JOIN users u ON u.id = a.user_id "
    "WHERE u.legacy_profile_b44_id IS NOT NULL AND a.is_default",
    "shop_menu_items": "SELECT count(*) FROM shop_menu_items m JOIN shops s ON s.id = m.shop_id "
    "WHERE s.legacy_b44_id IS NOT NULL",
    "files": "SELECT count(*) FROM files WHERE key = ANY(:keys)",
}


@dataclass
class Check:
    name: str
    ok: bool
    expected: Any = None
    actual: Any = None
    detail: str = ""


@dataclass
class VerifyResult:
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    def add(self, name: str, expected: Any, actual: Any, detail: str = "") -> None:
        self.checks.append(Check(name, expected == actual, expected, actual, detail))


def _num(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.001"))


async def _scalar(conn: AsyncConnection, sql: str, **params: Any) -> Any:
    return (await conn.execute(text(sql), params)).scalar()


async def check_counts(conn: AsyncConnection, bundle: Bundle, result: VerifyResult) -> None:
    report = bundle.report
    for entity, (tbl, col) in LEGACY_TABLES.items():
        kept = sorted(bundle.kept.get(entity, set()))
        in_db = await _scalar(conn, f"SELECT count(*) FROM {tbl} WHERE {col} = ANY(:ids)", ids=kept)
        result.add(f"rows {entity} -> {tbl}", len(kept), in_db)
        exported = report.export_counts.get(entity, 0)
        result.add(
            f"reconcile {entity}: export = kept + excluded",
            exported,
            len(kept) + report.excluded_count(entity),
            f"{report.excluded_count(entity)} excluded",
        )
    profiles = bundle.kept.get("UserProfile", set())
    merged = report.notes.get("duplicate UserProfile merged (most recent kept)", 0)
    in_db = await _scalar(
        conn, "SELECT count(*) FROM users WHERE legacy_profile_b44_id = ANY(:ids)", ids=sorted(profiles)
    )
    result.add(
        "rows UserProfile -> users (one per account, duplicates merged)", len(profiles) - merged, in_db
    )
    result.add(
        "reconcile UserProfile: export = kept + excluded",
        report.export_counts.get("UserProfile", 0),
        len(profiles) + report.excluded_count("UserProfile"),
    )
    for tbl, sql in DERIVED.items():
        params = {"keys": [f.key for f in bundle.files]} if tbl == "files" else {}
        result.add(f"rows {tbl}", len(bundle.rows(tbl)), await _scalar(conn, sql, **params))
    events = await _scalar(
        conn,
        "SELECT count(*) FROM order_status_events e JOIN orders o ON o.id = e.order_id "
        "WHERE o.legacy_b44_id IS NOT NULL",
    )
    result.checks.append(
        Check(
            "rows order_status_events (migrated history)",
            events >= len(bundle.rows("order_status_events")),
            len(bundle.rows("order_status_events")),
            events,
            "at least the migrated events",
        )
    )


async def check_statuses(conn: AsyncConnection, export: dict, bundle: Bundle, result: VerifyResult) -> None:
    kept_orders = bundle.kept.get("Order", set())
    expected = Counter(o["status"] for o in export["Order"] if o["id"] in kept_orders)
    rows = await conn.execute(
        text("SELECT status::text, count(*) FROM orders WHERE legacy_b44_id = ANY(:ids) GROUP BY 1"),
        {"ids": sorted(kept_orders)},
    )
    result.add("orders per status", dict(sorted(expected.items())), dict(sorted(rows.all())))

    offer_rows = {r["legacy_b44_id"]: r["status"] for r in bundle.rows("order_offers")}
    rows = await conn.execute(
        text("SELECT status::text, count(*) FROM order_offers WHERE legacy_b44_id = ANY(:ids) GROUP BY 1"),
        {"ids": sorted(offer_rows)},
    )
    result.add(
        "offers per status", dict(sorted(Counter(offer_rows.values()).items())), dict(sorted(rows.all()))
    )

    kept_notifications = bundle.kept.get("Notification", set())
    expected_types = Counter(
        NOTIFICATION_TYPE_SYNONYMS.get(n["type"], n["type"])
        for n in export["Notification"]
        if n["id"] in kept_notifications
    )
    rows = await conn.execute(
        text("SELECT type, count(*) FROM notifications WHERE legacy_b44_id = ANY(:ids) GROUP BY 1"),
        {"ids": sorted(kept_notifications)},
    )
    result.add(
        "notifications per type (synonyms merged)",
        dict(sorted(expected_types.items())),
        dict(sorted(rows.all())),
    )

    for tbl, entity in (("no_response_cases", "NoResponseCase"), ("hot_deals", "ResaleOrder")):
        expected_st = Counter(r["status"] for r in bundle.rows(tbl))
        rows = await conn.execute(
            text(f"SELECT status, count(*) FROM {tbl} WHERE legacy_b44_id = ANY(:ids) GROUP BY 1"),
            {"ids": sorted(bundle.kept.get(entity, set()))},
        )
        result.add(f"{tbl} per status", dict(sorted(expected_st.items())), dict(sorted(rows.all())))


def _bounded(value: Any, low: Decimal, high: Decimal | None, low_open: bool = False) -> Decimal:
    amount = money(value)
    if amount is None:
        return Decimal(0)
    if (amount <= low if low_open else amount < low) or (high is not None and amount > high):
        return Decimal(0)
    return amount


def _kept_amount(value: Any, high: Decimal | None) -> Decimal | None:
    """The column value the import keeps: the amount when within [0, high], else NULL."""
    amount = money(value)
    return amount if amount is not None and amount >= 0 and (high is None or amount <= high) else None


def _first_amount(amount: Decimal | None, fallback: Any) -> Decimal | None:
    return amount if amount is not None else money(fallback)


async def check_money(conn: AsyncConnection, export: dict, bundle: Bundle, result: VerifyResult) -> None:
    """Expected sums re-derived from the raw export with the documented bounds."""
    kept = bundle.kept.get("Order", set())
    orders = [o for o in export["Order"] if o["id"] in kept]
    ids = sorted(kept)
    for column, high in (("delivery_fee", Decimal(200)), ("purchase_amount", Decimal(2000))):
        expected = sum((_bounded(o.get(column), Decimal(0), high) for o in orders), Decimal(0))
        actual = await _scalar(
            conn,
            f"SELECT coalesce(sum({column}), 0) FROM orders WHERE legacy_b44_id = ANY(:ids)",
            ids=ids,
        )
        result.add(f"sum orders.{column}", _num(expected), _num(actual))
    raw_fee_total = sum((money(o.get("delivery_fee")) or Decimal(0) for o in orders), Decimal(0))
    result.checks.append(
        Check("sum orders.delivery_fee incl. out-of-range (export, info)", True, _num(raw_fee_total), None,
              "difference = fees set to NULL (audit_log)")
    )  # fmt: skip
    offers = [f for f in export["OrderOffer"] if f["id"] in bundle.kept.get("OrderOffer", set())]
    expected = sum(
        (_bounded(f.get("proposed_fee"), Decimal(0), Decimal(200), low_open=True) for f in offers), Decimal(0)
    )
    actual = await _scalar(
        conn,
        "SELECT coalesce(sum(proposed_fee), 0) FROM order_offers WHERE legacy_b44_id = ANY(:ids)",
        ids=sorted(bundle.kept.get("OrderOffer", set())),
    )
    result.add("sum order_offers.proposed_fee", _num(expected), _num(actual))
    deals = [d for d in export["ResaleOrder"] if d["id"] in bundle.kept.get("ResaleOrder", set())]
    rules = {
        # a deal without a usable discounted price is sold at its purchase amount
        "price": lambda d: _first_amount(
            _kept_amount(d.get("discounted_price"), None), d.get("purchase_amount")
        ),
        "purchase_amount": lambda d: money(d.get("purchase_amount")),
    }
    for column, rule in rules.items():
        expected = sum((rule(d) or Decimal(0) for d in deals), Decimal(0))
        actual = await _scalar(
            conn,
            f"SELECT coalesce(sum({column}), 0) FROM hot_deals WHERE legacy_b44_id = ANY(:ids)",
            ids=sorted(bundle.kept.get("ResaleOrder", set())),
        )
        result.add(f"sum hot_deals.{column}", _num(expected), _num(actual))
    delivered_with_fee = sum(
        1
        for o in orders
        if o["status"] == "delivered"
        and o.get("courier_id")
        and _bounded(o.get("delivery_fee"), Decimal(0), Decimal(200)) > 0
    )
    actual = (
        await conn.execute(
            text(
                "SELECT count(*), coalesce(sum(amount), 0), "
                "count(*) FILTER (WHERE kind <> 'commission_waived_launch') "
                "FROM courier_ledger_entries l JOIN orders o ON o.id = l.order_id "
                "WHERE o.legacy_b44_id IS NOT NULL"
            )
        )
    ).one()
    result.add(
        "ledger: one waived-launch entry per delivered order with a fee", delivered_with_fee, actual[0]
    )
    result.add(
        "ledger: nominal 0.500 each, nothing due",
        (_num(Decimal("0.5") * delivered_with_fee), 0),
        (_num(actual[1]), actual[2]),
    )


COURIER_STATUSES = (
    "'accepted','at_shop','price_confirmation_needed','purchased','on_the_way','delivered',"
    "'client_no_response'"
)
INTEGRITY = {
    "orders in a courier status without courier": (
        f"SELECT count(*) FROM orders WHERE status IN ({COURIER_STATUSES}) AND courier_id IS NULL"
    ),
    "delivered orders without delivered_at": (
        "SELECT count(*) FROM orders WHERE status = 'delivered' AND delivered_at IS NULL"
    ),
    "cancelled orders without cancelled_at": (
        "SELECT count(*) FROM orders WHERE status = 'cancelled' AND cancelled_at IS NULL"
    ),
    "migrated orders without any status event": (
        "SELECT count(*) FROM orders o WHERE o.legacy_b44_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM order_status_events e WHERE e.order_id = o.id)"
    ),
    "migrated orders whose last event is not their status": (
        "SELECT count(*) FROM orders o JOIN LATERAL (SELECT to_status FROM order_status_events e "
        "WHERE e.order_id = o.id ORDER BY created_at DESC, id DESC LIMIT 1) last ON true "
        "WHERE o.legacy_b44_id IS NOT NULL AND last.to_status <> o.status"
    ),
    "events whose from_status breaks the chain": (
        "SELECT count(*) FROM (SELECT from_status, lag(to_status) OVER "
        "(PARTITION BY order_id ORDER BY created_at, id) AS prev FROM order_status_events) x "
        "WHERE from_status IS DISTINCT FROM prev"
    ),
    "stops with a gap in seq": (
        "SELECT count(*) FROM (SELECT order_id, max(seq) + 1 AS n, count(*) AS c FROM order_stops "
        "GROUP BY order_id) x WHERE n <> c"
    ),
    "ratings whose courier is not the order's courier": (
        "SELECT count(*) FROM order_ratings r JOIN orders o ON o.id = r.order_id "
        "WHERE o.courier_id IS DISTINCT FROM r.courier_id"
    ),
    "accepted offers of another courier than the order's": (
        "SELECT count(*) FROM order_offers f JOIN orders o ON o.id = f.order_id "
        "WHERE f.status = 'accepted' AND o.status = 'delivered' AND o.courier_id <> f.courier_id"
    ),
    "waiting no-response cases on closed orders": (
        "SELECT count(*) FROM no_response_cases n JOIN orders o ON o.id = n.order_id "
        "WHERE n.status = 'waiting' AND o.status IN ('delivered','cancelled')"
    ),
    "sold hot deals without buyer": (
        "SELECT count(*) FROM hot_deals WHERE status = 'sold' AND buyer_id IS NULL"
    ),
    "couriers whose user is missing": (
        "SELECT count(*) FROM couriers c LEFT JOIN users u ON u.id = c.user_id WHERE u.id IS NULL"
    ),
    "foreign keys not validated": (
        "SELECT count(*) FROM pg_constraint WHERE contype = 'f' AND NOT convalidated"
    ),
    "users with more than one default address": (
        "SELECT count(*) FROM (SELECT user_id FROM user_addresses WHERE is_default "
        "GROUP BY 1 HAVING count(*) > 1) x"
    ),
}


async def check_integrity(conn: AsyncConnection, bundle: Bundle, result: VerifyResult) -> None:
    for name, sql in INTEGRITY.items():
        result.add(name, 0, await _scalar(conn, sql))
    expected_null_senders = bundle.report.adjusted.get(("messages", "sender_id", "unknown sender -> NULL"), 0)
    result.add(
        "messages without sender (documented unknown senders)",
        expected_null_senders,
        await _scalar(
            conn,
            "SELECT count(*) FROM messages WHERE legacy_b44_id = ANY(:ids) AND sender_id IS NULL",
            ids=sorted(bundle.kept.get("Message", set())),
        ),
    )


async def check_samples(
    conn: AsyncConnection, export: dict, bundle: Bundle, result: VerifyResult, n: int, seed: int
) -> None:
    kept = sorted(bundle.kept.get("Order", set()))
    picks = random.Random(seed).sample(kept, min(n, len(kept)))
    by_id = {o["id"]: o for o in export["Order"]}
    offers_per_order = Counter(r["order_id"] for r in bundle.rows("order_offers"))
    new_id = {r["legacy_b44_id"]: r["id"] for r in bundle.rows("orders")}
    mismatches: list[str] = []
    for legacy in picks:
        src = by_id[legacy]
        row = (
            await conn.execute(
                text(
                    "SELECT o.status::text AS status, lower(u.email) AS customer, "
                    "c.legacy_b44_id AS courier, "
                    "o.items_text, o.quantity, o.delivery_fee, o.purchase_amount, o.created_at, "
                    "o.contact_phone_e164, o.package::text AS package, "
                    "(SELECT count(*) FROM order_stops s WHERE s.order_id = o.id) AS stops, "
                    "(SELECT name FROM order_stops s WHERE s.order_id = o.id AND s.seq = 0) AS stop0, "
                    "(SELECT rating FROM order_ratings r WHERE r.order_id = o.id) AS rating, "
                    "(SELECT count(*) FROM order_offers f WHERE f.order_id = o.id) AS offers "
                    "FROM orders o JOIN users u ON u.id = o.customer_id "
                    "LEFT JOIN couriers c ON c.id = o.courier_id "
                    "WHERE o.legacy_b44_id = :id"
                ),
                {"id": legacy},
            )
        ).one()
        shops = src.get("shops") or []
        expected = {
            "status": src["status"],
            "customer": str(src["customer_id"]).lower(),
            "courier": src.get("courier_id") or None,
            "items_text": str(src["items_text"]).strip(),
            "quantity": min(100, max(1, int(src["quantity"] or 1))),
            "delivery_fee": _kept_amount(src.get("delivery_fee"), Decimal(200)),
            "purchase_amount": _kept_amount(src.get("purchase_amount"), Decimal(2000)),
            "created_at": parse_dt(src["created_date"]),
            "contact_phone_e164": normalize_phone(src.get("customer_phone"), ("CH",))[0],
            "package": src.get("package_size") or "petit",
            "stops": len(shops) if shops else (1 if src.get("shop_name") else 0),
            "stop0": ((shops[0].get("name") if shops else src.get("shop_name")) or "").strip() or None,
            "rating": src.get("customer_rating"),
            "offers": offers_per_order.get(new_id[legacy], 0),
        }  # fmt: skip
        actual = dict(row._mapping)
        for key, want in expected.items():
            got = actual[key]
            if isinstance(want, Decimal) or isinstance(got, Decimal):
                same = (want is None and got is None) or (
                    want is not None and got is not None and _num(want) == _num(got)
                )
            else:
                same = want == got
            if not same:
                mismatches.append(f"order {legacy}: {key}")
    result.checks.append(
        Check(f"sample of {len(picks)} random orders equal to the export after mapping", not mismatches,
              0, len(mismatches), "; ".join(mismatches[:10]))
    )  # fmt: skip


async def check_courier_stats(
    conn: AsyncConnection, export: dict, bundle: Bundle, result: VerifyResult
) -> None:
    kept = bundle.kept.get("Order", set())
    delivered = Counter(
        o["courier_id"]
        for o in export["Order"]
        if o["id"] in kept and o["status"] == "delivered" and o.get("courier_id")
    )
    ratings: dict[str, list[int]] = {}
    for o in export["Order"]:
        if o["id"] in kept and o.get("customer_rating") and o.get("courier_id"):
            ratings.setdefault(o["courier_id"], []).append(int(o["customer_rating"]))
    rows = (
        await conn.execute(
            text(
                "SELECT c.legacy_b44_id, s.total_deliveries, s.average_rating FROM courier_stats s "
                "JOIN couriers c ON c.id = s.courier_id WHERE c.legacy_b44_id IS NOT NULL"
            )
        )
    ).all()
    bad = []
    for legacy, total, average in rows:
        if total != delivered.get(legacy, 0):
            bad.append(f"{legacy}: deliveries")
        values = ratings.get(legacy)
        want = (Decimal(sum(values)) / len(values)).quantize(Decimal("0.01")) if values else None
        if (want is None) != (average is None) or (want is not None and want != Decimal(average)):
            bad.append(f"{legacy}: rating")
    result.checks.append(
        Check("courier_stats = export delivered orders and ratings", not bad, 0, len(bad), "; ".join(bad))
    )


def check_files(bundle: Bundle, result: VerifyResult) -> None:
    from botocore.exceptions import ClientError

    from app.config import settings
    from app.storage.s3 import server_client

    missing = 0
    for item in bundle.files:
        try:
            server_client().head_object(Bucket=settings.S3_BUCKET, Key=item.key)
        except ClientError:
            missing += 1
    result.add("files present in the bucket", 0, missing)


async def verify(
    export_dir: Path,
    database_url: str,
    *,
    bundle: Bundle | None = None,
    samples: int = 20,
    seed: int = 1,
    files: bool = False,
    fallback_regions: tuple[str, ...] = ("CH",),
) -> VerifyResult:
    export = load_export(export_dir)
    bundle = bundle or transform(export_dir, fallback_regions=fallback_regions)
    result = VerifyResult()
    engine = make_engine(database_url)
    try:
        async with engine.connect() as conn:
            await check_counts(conn, bundle, result)
            await check_statuses(conn, export, bundle, result)
            await check_money(conn, export, bundle, result)
            await check_integrity(conn, bundle, result)
            await check_samples(conn, export, bundle, result, samples, seed)
            await check_courier_stats(conn, export, bundle, result)
    finally:
        await engine.dispose()
    if files:
        await asyncio.to_thread(check_files, bundle, result)
    return result


def print_result(result: VerifyResult) -> None:
    for check in result.checks:
        mark = "OK  " if check.ok else "FAIL"
        extra = "" if check.ok else f" expected={check.expected} actual={check.actual}"
        detail = f" ({check.detail})" if check.detail else ""
        print(f"{mark} {check.name}{extra}{detail}")
    print("verify: GREEN" if result.ok else "verify: RED")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("export_dir", type=Path)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--check-files", action="store_true")
    parser.add_argument("--qa-constants", type=Path, default=None)
    args = parser.parse_args(argv)
    bundle = transform(args.export_dir, qa_emails=qa_emails_from_constants(args.qa_constants))
    result = asyncio.run(
        verify(
            args.export_dir,
            args.database_url,
            bundle=bundle,
            samples=args.samples,
            seed=args.seed,
            files=args.check_files,
        )
    )
    print_result(result)
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
