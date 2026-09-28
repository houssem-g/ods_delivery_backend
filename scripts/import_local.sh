#!/usr/bin/env bash
# make import-local [DB=ods_delivery] [EXPORT_DIR=…] [ARGS="--files"]
# Runs the Base44 import pipeline (docs/MIGRATION.md) on the most recent export found under
# ~/ODS-backups/migration/ (EXPORT_ROOT) — the directory with the latest name (YYYY-MM-DD) that
# holds entity JSON files — into a database of the compose db. Default ARGS: --files (the
# exported files are uploaded to the local MinIO bucket). The target must be migrated
# (make reset-local does it); the import is idempotent.
source "$(dirname "$0")/db_common.sh"

DB=${DB:-$MAIN_DB}
valid_db_name "$DB"
EXPORT_ROOT=${EXPORT_ROOT:-$HOME/ODS-backups/migration}
ARGS=${ARGS---files}

if [[ -z "${EXPORT_DIR:-}" ]]; then
  EXPORT_DIR=""
  while IFS= read -r dir; do
    compgen -G "$dir/*.json" > /dev/null && EXPORT_DIR=$dir
  done < <(find "$EXPORT_ROOT" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | sort)
  [[ -n "$EXPORT_DIR" ]] || die "no export directory with JSON files under $EXPORT_ROOT"
fi
[[ -d "$EXPORT_DIR" ]] || die "EXPORT_DIR '$EXPORT_DIR' is not a directory"
db_exists "$DB" || die "database '$DB' does not exist (make reset-local DB=$DB)"

echo "import: $EXPORT_DIR -> $DB ${ARGS:+($ARGS)}"
# shellcheck disable=SC2086 # ARGS is a list of options
uv run python -m migrate.pipeline "$EXPORT_DIR" --database-url "$(host_url "$DB")" $ARGS
