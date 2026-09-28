"""import / verify / report / pipeline against the test database (synthetic export only)."""

import importlib
import os
from pathlib import Path

import pytest
from sqlalchemy import text

from app.config import settings
from app.db import engine
from app.storage import s3
from migrate import importer, pipeline, report, verify
from migrate.transform import transform
from tests.migration import fixtures as fx

DB = os.environ["DATABASE_URL"]


@pytest.fixture
def export_dir(tmp_path) -> Path:
    return fx.write_export(tmp_path / "export")


def bundle_of(export_dir):
    return transform(export_dir, qa_emails={fx.QA})


async def count(sql: str, **params) -> int:
    async with engine.connect() as conn:
        return (await conn.execute(text(sql), params)).scalar()


async def test_import_then_rerun_changes_nothing(export_dir):
    first = await importer.import_bundle(bundle_of(export_dir), DB)
    assert first.tables["orders"].inserted == 10 and first.changes >= 100
    assert first.tables["orders_resale_links"].updated == 1
    assert await count("SELECT count(*) FROM orders") == 10
    assert await count("SELECT count(*) FROM order_status_events") == len(
        bundle_of(export_dir).rows("order_status_events")
    )
    assert await count("SELECT count(*) FROM shops WHERE place_id IS NOT NULL") == 1
    assert await count("SELECT count(*) FROM shop_reviews WHERE place_id IS NOT NULL") == 1
    assert await count("SELECT count(*) FROM users WHERE referred_by_courier_id IS NOT NULL") == 1
    assert await count("SELECT count(*) FROM couriers WHERE phone_e164 IS NULL") == 1

    second = await importer.import_bundle(bundle_of(export_dir), DB)
    assert second.changes == 0
    assert all(s.unchanged == s.rows for s in second.tables.values())
    assert await count("SELECT count(*) FROM order_status_events") == len(
        bundle_of(export_dir).rows("order_status_events")
    )
    assert await count("SELECT count(*) FROM audit_log") == len(bundle_of(export_dir).rows("audit_log"))


async def test_changed_source_row_is_updated_and_app_rows_untouched(export_dir):
    await importer.import_bundle(bundle_of(export_dir), DB)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE orders SET items_text = 'edited' WHERE legacy_b44_id = :id"),
            {"id": fx.bid("Order", "o1")},
        )
        await conn.execute(
            text("INSERT INTO users (email, full_name) VALUES ('new.app.user@example.test', 'New')")
        )
    stats = await importer.import_bundle(bundle_of(export_dir), DB)
    assert stats.changes == 1 and stats.tables["orders"].updated == 1
    assert await count("SELECT count(*) FROM users WHERE email = 'new.app.user@example.test'") == 1


async def test_dry_run_rolls_back(export_dir):
    stats = await importer.import_bundle(bundle_of(export_dir), DB, dry_run=True)
    assert stats.dry_run and stats.tables["orders"].inserted == 10
    assert await count("SELECT count(*) FROM orders") == 0


async def test_existing_accounts_are_adopted(export_dir):
    async with engine.begin() as conn:
        existing = (
            await conn.execute(
                text(
                    "INSERT INTO users (email, full_name, role) VALUES (:e, 'Seeded', 'courier') RETURNING id"
                ),
                {"e": fx.COUR1},
            )
        ).scalar()
        courier = (
            await conn.execute(
                text(
                    "INSERT INTO couriers (user_id, display_name, phone_e164, id_document_number, vehicle, "
                    "price_per_km) VALUES (:u, 'Seeded', '+21655667788', 'X', 'car', 1) RETURNING id"
                ),
                {"u": existing},
            )
        ).scalar()
    stats = await importer.import_bundle(bundle_of(export_dir), DB)
    assert (stats.adopted_users, stats.adopted_couriers) == (1, 1)
    assert await count("SELECT count(*) FROM users WHERE email = :e", e=fx.COUR1) == 1
    assert await count("SELECT count(*) FROM orders WHERE courier_id = :c", c=courier) == 6
    assert (
        await count("SELECT count(*) FROM couriers WHERE id = :c AND legacy_b44_id = :l", c=courier, l=fx.CP1)
        == 1
    )
    again = await importer.import_bundle(bundle_of(export_dir), DB)
    assert again.changes == 0


async def test_account_of_another_base44_user_is_refused(export_dir):
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (email, full_name, legacy_b44_id) "
                "VALUES (:e, 'Other', 'ffffffffffffffffffffffff')"
            ),
            {"e": fx.CUST1},
        )
    with pytest.raises(importer.ImportRefused):
        await importer.import_bundle(bundle_of(export_dir), DB)


async def test_courier_of_another_base44_profile_is_refused(export_dir):
    bundle = bundle_of(export_dir)
    user = next(u for u in bundle.rows("users") if u["email"] == fx.COUR1)
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO users (id, email, full_name, legacy_b44_id) VALUES (:id, :e, 'K', :l)"),
            {"id": user["id"], "e": fx.COUR1, "l": user["legacy_b44_id"]},
        )
        await conn.execute(
            text(
                "INSERT INTO couriers (user_id, display_name, id_document_number, vehicle, price_per_km, "
                "legacy_b44_id) VALUES (:u, 'K', 'X', 'car', 1, 'eeeeeeeeeeeeeeeeeeeeeeee')"
            ),
            {"u": user["id"]},
        )
    with pytest.raises(importer.ImportRefused):
        await importer.import_bundle(bundle, DB)


async def test_verify_is_green_and_detects_drift(export_dir):
    await importer.import_bundle(bundle_of(export_dir), DB)
    result = await verify.verify(export_dir, DB, samples=50)
    assert result.ok, [c for c in result.checks if not c.ok]
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE orders SET delivery_fee = 99 WHERE legacy_b44_id = :id"),
            {"id": fx.bid("Order", "o1")},
        )
        await conn.execute(
            text("DELETE FROM messages WHERE legacy_b44_id = :id"), {"id": fx.bid("Message", "m1")}
        )
    result = await verify.verify(export_dir, DB, samples=50)
    failed = {c.name for c in result.checks if not c.ok}
    assert "sum orders.delivery_fee" in failed and "rows Message -> messages" in failed
    assert any(name.startswith("sample of") for name in failed)
    verify.print_result(result)


async def test_report_render_and_write(export_dir, tmp_path):
    bundle = bundle_of(export_dir)
    stats = await importer.import_bundle(bundle, DB)
    result = await verify.verify(export_dir, DB)
    content = report.render(bundle, export_dir, DB, stats, result)
    assert "Verify: **GREEN**" in content and "ods_delivery_test" in content
    assert "ods_delivery_local" not in content  # no password
    for email in (fx.CUST1, fx.COUR1, fx.ADMIN):
        assert email not in content
    assert "cou…@example.test" in content or "cus…@example.test" in content
    path = report.write_report(tmp_path / "out" / "_report.md", content)
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(SystemExit):
        report.write_report(Path(report.REPO) / "docs" / "_report.md", content)
    assert "Not run." in report.render(bundle, tmp_path, DB, None, None)


async def test_pipeline_and_import_cli(export_dir, capsys):
    assert await pipeline.run(pipeline_args(export_dir, dry_run=True)) == 0
    assert await pipeline.run(pipeline_args(export_dir)) == 0
    out = capsys.readouterr().out
    assert "verify: GREEN" in out and (export_dir / "_report.md").stat().st_mode & 0o777 == 0o600
    assert await pipeline.run(pipeline_args(export_dir)) == 0
    assert "import applied: 0 row changes" in capsys.readouterr().out
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE hot_deals SET price = 1"))
    # the rerun repairs the drift (upsert) and verify is green again
    assert await pipeline.run(pipeline_args(export_dir)) == 0


def pipeline_args(export_dir, **overrides):
    import argparse

    values = {
        "export_dir": export_dir,
        "database_url": DB,
        "dry_run": False,
        "files": False,
        "report": None,
        "qa_constants": None,
        "fallback_region": None,
        "samples": 20,
        "seed": 1,
    } | overrides
    return argparse.Namespace(**values)


def test_import_cli_dry_run(export_dir, capsys):
    import_cli = importlib.import_module("migrate.import")
    assert import_cli.main([str(export_dir), "--database-url", DB, "--dry-run"]) == 0
    assert "DRY RUN" in capsys.readouterr().out


def test_verify_cli_on_empty_database(export_dir, capsys):
    assert verify.main([str(export_dir), "--database-url", DB, "--samples", "0"]) == 1
    assert "verify: RED" in capsys.readouterr().out


def test_pipeline_main_dry_run(export_dir):
    assert (
        pipeline.main(
            [str(export_dir), "--database-url", DB, "--dry-run", "--report", str(export_dir / "r.md")]
        )
        == 0
    )


@pytest.fixture
def bucket():
    client = s3.server_client()
    try:
        names = {b["Name"] for b in client.list_buckets()["Buckets"]}
    except Exception:
        pytest.skip("MinIO is not running")
    if settings.S3_BUCKET not in names:
        client.create_bucket(Bucket=settings.S3_BUCKET)
    return client


async def test_files_are_uploaded_once(export_dir, bucket):
    bundle = bundle_of(export_dir)
    for item in bundle.files:
        bucket.delete_object(Bucket=settings.S3_BUCKET, Key=item.key)  # absent keys are not an error
    first = await importer.import_bundle(bundle, DB, files=True)
    assert (first.files_uploaded, first.files_present) == (2, 0)
    head = bucket.head_object(Bucket=settings.S3_BUCKET, Key=bundle.files[0].key)
    assert head["ContentLength"] == bundle.files[0].size
    second = await importer.import_bundle(bundle_of(export_dir), DB, files=True)
    assert (second.files_uploaded, second.files_present) == (0, 2)
    result = await verify.verify(export_dir, DB, files=True)
    assert next(c for c in result.checks if c.name == "files present in the bucket").ok
    importer.print_stats(second)
