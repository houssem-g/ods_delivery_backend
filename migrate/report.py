"""Markdown migration report (layout: docs/MIGRATION_REPORT_TEMPLATE.md).

Aggregates only: counts, sums, reasons, masked examples. The report is written next to the
export (mode 600), never inside the repository.
"""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.engine import make_url

from migrate.bundle import TABLE_ORDER, Bundle
from migrate.importer import ImportStats
from migrate.verify import VerifyResult

REPO = Path(__file__).resolve().parents[1]


def _table(headers: list[str], rows: list[list[object]]) -> list[str]:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return out


def _target(database_url: str) -> str:
    url = make_url(database_url)
    return f"{url.host}:{url.port}/{url.database}"


def render(
    bundle: Bundle,
    export_dir: Path,
    database_url: str,
    stats: ImportStats | None,
    result: VerifyResult | None,
) -> str:
    report = bundle.report
    summary_path = export_dir / "_summary.json"
    exported_at = "?"
    if summary_path.is_file():
        exported_at = json.loads(summary_path.read_text(encoding="utf-8")).get("exported_at", "?")
    lines = [
        "# Base44 → PostgreSQL migration report",
        "",
        f"- Generated: {datetime.now(UTC):%Y-%m-%d %H:%M} UTC",
        f"- Export: `{export_dir.name}` (exported at {exported_at})",
        f"- Target: `{_target(database_url)}`"
        + (" — **dry run (rolled back)**" if stats and stats.dry_run else ""),
        f"- Verify: **{'GREEN' if result and result.ok else ('RED' if result else 'not run')}**",
        "",
        "## 1. Entities: export → kept / excluded",
        "",
    ]
    rows = []
    for entity, count in report.export_counts.items():
        kept = len(bundle.kept.get(entity, set()))
        rows.append([entity, count, kept, report.excluded_count(entity)])
    lines += _table(["Entity", "Export rows", "Kept", "Excluded"], rows)
    lines += ["", "## 2. Target tables", ""]
    trows = []
    for name in TABLE_ORDER:
        s = stats.tables.get(name) if stats else None
        trows.append(
            [name, len(bundle.rows(name)), *([s.inserted, s.updated, s.unchanged] if s else ["-", "-", "-"])]
        )
    lines += _table(["Table", "Rows", "Inserted", "Updated", "Unchanged"], trows)
    if stats:
        lines += [
            "",
            f"Row changes: **{stats.changes}**. Adopted existing accounts: {stats.adopted_users} users, "
            f"{stats.adopted_couriers} couriers. Files: {len(bundle.files)} in the export, "
            f"{stats.files_uploaded} uploaded, {stats.files_present} already in the bucket.",
        ]
    lines += ["", "## 3. Exclusions (rows not migrated)", ""]
    lines += (
        _table(["Entity", "Reason", "Rows"], [[e, r, n] for (e, r), n in sorted(report.excluded.items())])
        if report.excluded
        else ["None."]
    )
    lines += ["", "## 4. Values changed on the way", ""]
    lines += (
        _table(
            ["Table", "Column", "Change", "Rows"],
            [[t, c, w, n] for (t, c, w), n in sorted(report.adjusted.items())],
        )
        if report.adjusted
        else ["None."]
    )
    lines += ["", "## 5. Anomalies and notes", ""]
    lines += (
        _table(["Observation", "Count"], [[k, n] for k, n in sorted(report.notes.items())])
        if report.notes
        else ["None."]
    )
    lines += ["", "## 6. Verify", ""]
    if result:
        vrows = []
        for check in result.checks:
            status = "OK" if check.ok else "**FAIL**"
            numbers = "" if check.ok else f"expected {check.expected}, got {check.actual}"
            vrows.append([check.name, status, numbers or check.detail])
        lines += _table(["Check", "Result", "Detail"], vrows)
    else:
        lines.append("Not run.")
    lines += ["", "## 7. Masked examples", ""]
    for key, examples in sorted(report.examples.items()):
        lines.append(f"- {key}: " + ", ".join(f"`{e}`" for e in examples))
    if not report.examples:
        lines.append("None.")
    lines.append("")
    return "\n".join(lines)


def write_report(path: Path, content: str) -> Path:
    if path.resolve().is_relative_to(REPO):
        raise SystemExit("refusing to write the migration report inside the repository")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)
    path.chmod(0o600)
    return path
