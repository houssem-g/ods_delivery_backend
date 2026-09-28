#!/usr/bin/env bash
# make backup [DB=ods_delivery]
# pg_dump (custom format, compressed) of a database of the compose db into backups/
# (git-ignored, mode 700), named <db>_<UTC timestamp>.dump. Read-only for the database.
source "$(dirname "$0")/db_common.sh"

DB=${DB:-$MAIN_DB}
valid_db_name "$DB"
db_exists "$DB" || die "database '$DB' does not exist"

BACKUP_DIR=${BACKUP_DIR:-backups}
umask 077
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
out="$BACKUP_DIR/${DB}_$(date -u +%Y%m%dT%H%M%SZ).dump"
trap 'rm -f "$out.partial"' EXIT

$COMPOSE exec -T db pg_dump -U "$PG_USER" -d "$DB" --format=custom --compress=6 > "$out.partial"
# The archive must be readable before it counts as a backup.
$COMPOSE exec -T db pg_restore --list < "$out.partial" > /dev/null || die "pg_restore cannot read the dump"
mv "$out.partial" "$out"
echo "backup: $out ($(du -h "$out" | cut -f1))"
