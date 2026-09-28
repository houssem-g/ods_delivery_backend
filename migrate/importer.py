"""Idempotent load of a transformed Bundle into PostgreSQL (the logic behind `migrate/import.py`).

- Tables are written in FK order (bundle.TABLE_ORDER), one transaction per table.
- Upsert: INSERT … ON CONFLICT (<legacy_b44_id or the table's natural key>) DO UPDATE … WHERE the
  row differs, so a second run changes nothing and reports 0 inserted / 0 updated.
- Never deletes. Rows that exist only in the database (created by the new app) are untouched.
- Accounts that already exist with the same e-mail (e.g. the local seed's QA accounts) are
  adopted: the import keeps their id and re-points every migrated reference to it.
- `dry_run`: everything runs in one transaction that is rolled back.
"""

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Table, literal_column, select, text, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app.models import Base
from migrate.bundle import TABLE_ORDER, Bundle, FileToUpload

CHUNK = 500
# conflict target per table (legacy id where the table has one, else its natural key)
CONFLICT = {
    "users": ("id",),  # ids resolved first (legacy id, then e-mail), see resolve_identities
    "couriers": ("id",),
    "user_addresses": ("id",),
    "files": ("key",),
    "places": ("osm_id",),  # the OSM refresh job may already have the place
    "shops": ("legacy_b44_id",),
    "shop_menu_items": ("id",),
    "shop_reviews": ("legacy_b44_id",),
    "orders": ("legacy_b44_id",),
    "order_stops": ("order_id", "seq"),
    "order_offers": ("legacy_b44_id",),
    "order_tracking": ("order_id",),
    "order_ratings": ("order_id",),
    "order_issues": ("id",),
    "messages": ("legacy_b44_id",),
    "notifications": ("legacy_b44_id",),
    "device_tokens": ("legacy_b44_id",),
    "no_response_cases": ("legacy_b44_id",),
    "hot_deals": ("legacy_b44_id",),
    "app_settings": ("key",),
    "courier_ledger_entries": ("order_id", "kind"),
}
# never overwritten on update: the id of an existing row, and what the app maintains itself
KEEP_ON_UPDATE = {"id", "updated_at"}
AUDIT_ACTIONS = ("b44_migration.adjust", "b44_migration.exclude")


class ImportRefused(RuntimeError):
    pass


@dataclass
class TableStats:
    rows: int = 0
    inserted: int = 0
    updated: int = 0

    @property
    def unchanged(self) -> int:
        return self.rows - self.inserted - self.updated


@dataclass
class ImportStats:
    tables: dict[str, TableStats] = field(default_factory=dict)
    adopted_users: int = 0
    adopted_couriers: int = 0
    files_uploaded: int = 0
    files_present: int = 0
    dry_run: bool = False

    @property
    def changes(self) -> int:
        return sum(s.inserted + s.updated for s in self.tables.values())


def table(name: str) -> Table:
    return Base.metadata.tables[name]


def _fk_columns(target: str) -> list[tuple[str, str]]:
    """(table, column) of every foreign key pointing at `target`.id."""
    out = []
    for tbl in Base.metadata.sorted_tables:
        for fk in tbl.foreign_keys:
            if fk.column.table.name == target and fk.column.name == "id":
                out.append((tbl.name, fk.parent.name))
    return out


def remap(bundle: Bundle, target: str, mapping: dict[Any, Any]) -> None:
    """Replaces migrated ids of `target` by the ids of rows adopted in the database."""
    if not mapping:
        return
    columns = _fk_columns(target)
    for row in bundle.rows(target):
        row["id"] = mapping.get(row["id"], row["id"])
    extra = {
        "users": [("users_referrals", "id")],
        "couriers": [("users_referrals", "referred_by_courier_id")],
    }
    for tbl, col in columns + extra.get(target, []):
        for row in bundle.rows(tbl):
            if row.get(col) in mapping:
                row[col] = mapping[row[col]]


async def resolve_identities(conn: AsyncConnection, bundle: Bundle, stats: ImportStats) -> None:
    """Existing accounts win: same legacy id first, else same e-mail (only if not migrated from
    another Base44 row). Couriers follow their user."""
    users = table("users")
    rows = bundle.rows("users")
    existing = (
        await conn.execute(
            select(users.c.id, users.c.email, users.c.legacy_b44_id).where(
                users.c.legacy_b44_id.in_([r["legacy_b44_id"] for r in rows])
                | users.c.email.in_([r["email"] for r in rows])
            )
        )
    ).all()
    by_legacy = {e.legacy_b44_id: e for e in existing if e.legacy_b44_id}
    by_email = {e.email.lower(): e for e in existing}
    mapping: dict[Any, Any] = {}
    for row in rows:
        found = by_legacy.get(row["legacy_b44_id"]) or by_email.get(row["email"].lower())
        if found is None:
            continue
        if found.legacy_b44_id not in (None, row["legacy_b44_id"]):
            raise ImportRefused("an existing account with a migrated e-mail comes from another Base44 user")
        if found.id != row["id"]:
            mapping[row["id"]] = found.id
            stats.adopted_users += 1
    remap(bundle, "users", mapping)

    couriers = table("couriers")
    crow = bundle.rows("couriers")
    existing_c = (
        await conn.execute(
            select(couriers.c.id, couriers.c.user_id, couriers.c.legacy_b44_id).where(
                couriers.c.legacy_b44_id.in_([r["legacy_b44_id"] for r in crow])
                | couriers.c.user_id.in_([r["user_id"] for r in crow])
            )
        )
    ).all()
    c_legacy = {e.legacy_b44_id: e for e in existing_c if e.legacy_b44_id}
    c_user = {e.user_id: e for e in existing_c}
    cmap: dict[Any, Any] = {}
    for row in crow:
        found = c_legacy.get(row["legacy_b44_id"]) or c_user.get(row["user_id"])
        if found is None:
            continue
        if found.legacy_b44_id not in (None, row["legacy_b44_id"]):
            raise ImportRefused("an existing courier of a migrated user comes from another Base44 profile")
        if found.id != row["id"]:
            cmap[row["id"]] = found.id
            stats.adopted_couriers += 1
    remap(bundle, "couriers", cmap)


def _clean(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]


async def upsert(conn: AsyncConnection, name: str, rows: list[dict[str, Any]]) -> TableStats:
    stats = TableStats(rows=len(rows))
    if not rows:
        return stats
    tbl = table(name)
    conflict = CONFLICT[name]
    columns = list(rows[0].keys())
    settable = [c for c in columns if c not in KEEP_ON_UPDATE and c not in conflict]
    for start in range(0, len(rows), CHUNK):
        chunk = rows[start : start + CHUNK]
        stmt = insert(tbl).values(chunk)
        if settable:
            stmt = stmt.on_conflict_do_update(
                index_elements=list(conflict),
                set_={c: stmt.excluded[c] for c in settable},
                where=tuple_(*[tbl.c[c] for c in settable]).is_distinct_from(
                    tuple_(*[stmt.excluded[c] for c in settable])
                ),
            )
        else:
            stmt = stmt.on_conflict_do_nothing(index_elements=list(conflict))
        result = await conn.execute(stmt.returning(literal_column("xmax = 0").label("inserted")))
        flags = [r.inserted for r in result]
        stats.inserted += sum(1 for f in flags if f)
        stats.updated += sum(1 for f in flags if not f)
    return stats


async def resolve_place_ids(conn: AsyncConnection, rows: list[dict[str, Any]]) -> None:
    osm_ids = {r["_place_osm_id"] for r in rows if r.get("_place_osm_id")}
    if not osm_ids:
        return
    places = table("places")
    found = dict(
        (await conn.execute(select(places.c.osm_id, places.c.id).where(places.c.osm_id.in_(osm_ids)))).all()
    )
    for row in rows:
        row["place_id"] = found.get(row.get("_place_osm_id"))


async def link(conn: AsyncConnection, name: str, column: str, rows: list[dict[str, Any]]) -> TableStats:
    """Second pass for circular references: UPDATE <name> SET <column> where it differs."""
    stats = TableStats(rows=len(rows))
    tbl = table(name)
    for row in rows:
        result = await conn.execute(
            update(tbl)
            .where(tbl.c.id == row["id"], tbl.c[column].is_distinct_from(row[column]))
            .values({column: row[column]})
        )
        stats.updated += result.rowcount
    return stats


async def insert_events(conn: AsyncConnection, rows: list[dict[str, Any]]) -> TableStats:
    """Events are append-only without a legacy key: an order's history is written once, when
    it has no event yet (a rerun, or events the new app appended since, leave it alone)."""
    stats = TableStats(rows=len(rows))
    order_ids = list({r["order_id"] for r in rows})
    events = table("order_status_events")
    have: set[Any] = set()
    for start in range(0, len(order_ids), CHUNK):
        part = order_ids[start : start + CHUNK]
        have |= set(
            (
                await conn.execute(select(events.c.order_id).where(events.c.order_id.in_(part)).distinct())
            ).scalars()
        )
    todo = [r for r in rows if r["order_id"] not in have]
    for start in range(0, len(todo), CHUNK):
        await conn.execute(insert(events).values(todo[start : start + CHUNK]))
    stats.inserted = len(todo)
    return stats


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


async def insert_audit(conn: AsyncConnection, rows: list[dict[str, Any]]) -> TableStats:
    stats = TableStats(rows=len(rows))
    audit = table("audit_log")
    existing = await conn.execute(
        select(audit.c.action, audit.c.entity, audit.c.entity_id, audit.c.before, audit.c.after).where(
            audit.c.action.in_(AUDIT_ACTIONS)
        )
    )
    seen = {(r.action, r.entity, r.entity_id, _canonical(r.before), _canonical(r.after)) for r in existing}
    todo = []
    for row in rows:
        key = (
            row["action"],
            row["entity"],
            row["entity_id"],
            _canonical(row["before"]),
            _canonical(row["after"]),
        )
        if key not in seen:
            seen.add(key)
            todo.append(row)
    if todo:
        await conn.execute(insert(audit).values(todo))
    stats.inserted = len(todo)
    return stats


async def check_schema(conn: AsyncConnection) -> str:
    try:
        version = (
            await conn.execute(text("SELECT string_agg(version_num, ',') FROM alembic_version"))
        ).scalar()
    except Exception as exc:  # pragma: no cover - message only
        raise ImportRefused("the target database has no schema: run `alembic upgrade head` first") from exc
    if not version:
        raise ImportRefused("the target database has no schema: run `alembic upgrade head` first")
    return version


async def run_step(conn: AsyncConnection, name: str, bundle: Bundle) -> TableStats:
    rows = bundle.rows(name)
    if name == "users_referrals":
        return await link(conn, "users", "referred_by_courier_id", rows)
    if name == "orders_resale_links":
        return await link(conn, "orders", "resale_deal_id", rows)
    if name == "order_status_events":
        return await insert_events(conn, rows)
    if name == "audit_log":
        return await insert_audit(conn, rows)
    if name in ("shops", "shop_reviews"):
        await resolve_place_ids(conn, rows)
    return await upsert(conn, name, _clean(rows))


def upload_files(files: list[FileToUpload], stats: ImportStats) -> None:
    """Puts the exported files in the bucket (app/storage settings), skipping objects already there."""
    from botocore.exceptions import ClientError

    from app.config import settings
    from app.storage.s3 import server_client

    client = server_client()
    for item in files:
        try:
            head = client.head_object(Bucket=settings.S3_BUCKET, Key=item.key)
            if head.get("ContentLength") == item.size:
                stats.files_present += 1
                continue
        except ClientError:
            pass
        client.put_object(
            Bucket=settings.S3_BUCKET,
            Key=item.key,
            Body=item.path.read_bytes(),
            ContentType=item.content_type,
        )
        stats.files_uploaded += 1


def make_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(database_url, poolclass=NullPool)


async def import_bundle(
    bundle: Bundle, database_url: str, *, dry_run: bool = False, files: bool = False
) -> ImportStats:
    stats = ImportStats(dry_run=dry_run)
    engine = make_engine(database_url)
    try:
        if files and not dry_run:
            await asyncio.to_thread(upload_files, bundle.files, stats)
        async with engine.connect() as conn:
            await check_schema(conn)
            await conn.rollback()
            outer = await conn.begin()
            await resolve_identities(conn, bundle, stats)
            if dry_run:
                for name in TABLE_ORDER:
                    stats.tables[name] = await run_step(conn, name, bundle)
                await outer.rollback()
                return stats
            await outer.commit()
            for name in TABLE_ORDER:
                async with conn.begin():
                    stats.tables[name] = await run_step(conn, name, bundle)
    finally:
        await engine.dispose()
    return stats


def print_stats(stats: ImportStats) -> None:
    label = "DRY RUN (rolled back)" if stats.dry_run else "applied"
    print(f"import {label}: {stats.changes} row changes")
    print(f"{'table':24} {'rows':>6} {'inserted':>9} {'updated':>8} {'unchanged':>10}")
    for name, s in stats.tables.items():
        print(f"{name:24} {s.rows:6} {s.inserted:9} {s.updated:8} {s.unchanged:10}")
    if stats.adopted_users or stats.adopted_couriers:
        print(f"adopted existing accounts: {stats.adopted_users} users, {stats.adopted_couriers} couriers")
    if stats.files_uploaded or stats.files_present:
        print(f"files: {stats.files_uploaded} uploaded, {stats.files_present} already in the bucket")
