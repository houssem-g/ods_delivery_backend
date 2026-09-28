#!/usr/bin/env bash
# make reset-local [DB=ods_delivery] [YES=1]
# Drops and recreates a database of the compose db (extensions of docker/db/init.sql),
# then `alembic upgrade head` and the local seed (admin, QA accounts, default settings).
# Asks to type the database name unless YES=1. Take a `make backup` first if the data matters.
source "$(dirname "$0")/db_common.sh"

DB=${DB:-$MAIN_DB}
valid_db_name "$DB"
case "$DB" in ods_delivery_test*) die "'$DB' is a pytest database (pytest resets it itself)";; esac

if [[ "${YES:-0}" != "1" ]]; then
  [[ -t 0 ]] || die "not a terminal: add YES=1 to confirm"
  echo "This DROPS database '$DB' (every row, imported Base44 data included) and recreates it empty."
  db_exists "$DB" && echo "Backup first? make backup DB=$DB"
  read -r -p "Type the database name to confirm: " answer
  [[ "$answer" == "$DB" ]] || die "not confirmed"
fi

drop_db "$DB"
create_db_with_extensions "$DB"
url=$(host_url "$DB")
DATABASE_URL="$url" uv run alembic upgrade head
DATABASE_URL="$url" uv run python -m scripts.seed_local
echo "database '$DB' reset: migrated to head and seeded"
