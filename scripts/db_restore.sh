#!/usr/bin/env bash
# make restore FILE=backups/x.dump DB=<new database name> [FORCE=1]
# Restores a custom-format dump into a NEW database. Refuses an existing database
# (and always ods_delivery) unless FORCE=1, which drops it first (connections are cut).
source "$(dirname "$0")/db_common.sh"

FILE=${FILE:-}
DB=${DB:-}
[[ -n "$FILE" && -f "$FILE" ]] || die "FILE=<path of a .dump> is required (got '${FILE}')"
[[ -n "$DB" ]] || die "DB=<new database name> is required (e.g. DB=ods_delivery_restore_check)"
valid_db_name "$DB"

if [[ "$DB" == "$MAIN_DB" && "${FORCE:-0}" != "1" ]]; then
  die "refusing to overwrite '$MAIN_DB' (the local app database); add FORCE=1 to really do it"
fi
if db_exists "$DB"; then
  [[ "${FORCE:-0}" == "1" ]] || die "database '$DB' already exists; pick a new name or add FORCE=1"
  echo "dropping existing database '$DB' (FORCE=1)"
  drop_db "$DB"
fi

psql_db postgres -c "CREATE DATABASE \"$DB\" OWNER $PG_USER TEMPLATE template0"
# The dump carries its extensions (postgis, citext, …); owners are not restored.
$COMPOSE exec -T db pg_restore -U "$PG_USER" -d "$DB" --no-owner --no-privileges --exit-on-error < "$FILE"
echo "restored $FILE into '$DB'"
scripts/db_counts.sh "$DB"
