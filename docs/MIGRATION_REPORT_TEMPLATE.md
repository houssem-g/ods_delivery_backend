# Migration report — template

`migrate/report.py` writes this report after each `make import-base44` run, **next to the
export** (`<export dir>/_report.md`, mode 600, refused inside the repository). It holds
aggregates only: counts, sums, reasons, legacy ids at most, and masked examples
(`abc…@domain`, `9 digits, …89`). No names, e-mails, phones or addresses in clear.

## Header
- generation time (UTC), export folder and its `exported_at` (from `_summary.json`);
- target `host:port/database` (never the password), and whether the run was a dry run;
- overall verify result: GREEN / RED / not run.

## 1. Entities: export → kept / excluded
One row per Base44 entity (all 16, empty ones included): rows in the export, rows kept,
rows excluded. `kept + excluded = export` for every entity (verify checks it).

## 2. Target tables
One row per step of the import in FK order (bundle.TABLE_ORDER, including the two
second-pass links): rows produced by the transform, inserted, updated, unchanged. Below:
total row changes (0 on a rerun), adopted existing accounts, files in the export /
uploaded / already in the bucket.

## 3. Exclusions
(entity, reason, rows) for every excluded row — the reasons are the ones of
docs/MIGRATION.md §3.

## 4. Values changed on the way
(table, column, change, rows): phones rejected or read in a fallback region, values
clamped or set to NULL for a CHECK (originals in `audit_log`), synthetic timestamps,
synonym types, inferred recipients, repaired ids, unified categories, …

## 5. Anomalies and notes
Counters that are not changes: merged duplicate profiles, orders without history or
`shops[]`, histories whose last item was not the status, empty `name_norm`, legacy ids left
in notification metadata, QA share (QA accounts found, orders placed / handled by them,
notifications to them).

## 6. Verify
Every check of `migrate/verify.py` with OK / FAIL; for a failure the expected and actual
values (aggregates) and the detail (legacy order ids of a failed sample at most).

## 7. Masked examples
Up to three masked examples per reason / change, to recognize a case without exposing it.
