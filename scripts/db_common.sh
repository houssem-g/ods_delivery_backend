# Shared helpers of the db_*.sh scripts (sourced, not run). Everything goes through the
# compose `db` service, so the host needs no PostgreSQL client of the right version.
#   COMPOSE   compose command (default: docker compose; another stack: "docker compose -p x")
#   DB_PORT   host port of the db, for the host-side tools (alembic, seed, import)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

COMPOSE=${COMPOSE:-docker compose}
PG_USER=ods_delivery
MAIN_DB=ods_delivery
DB_PORT=${DB_PORT:-5451}
POSTGRES_PASSWORD=${POSTGRES_PASSWORD:-ods_delivery_local}

die() { echo "error: $*" >&2; exit 1; }

valid_db_name() {
  [[ "$1" =~ ^[a-z_][a-z0-9_]{0,62}$ ]] || die "invalid database name '$1' (lowercase letters, digits, _)"
}

psql_db() { # psql_db <db> [psql args…]
  local db=$1; shift
  $COMPOSE exec -T db psql -v ON_ERROR_STOP=1 -X -q -U "$PG_USER" -d "$db" "$@"
}

db_exists() {
  [[ "$(psql_db postgres -tAc "SELECT 1 FROM pg_database WHERE datname = '$1'")" == "1" ]]
}

host_url() { # SQLAlchemy URL of <db> as seen from the host
  echo "postgresql+asyncpg://$PG_USER:$POSTGRES_PASSWORD@localhost:$DB_PORT/$1"
}

create_db_with_extensions() {
  psql_db postgres -c "CREATE DATABASE \"$1\" OWNER $PG_USER TEMPLATE template0"
  psql_db "$1" -c "CREATE EXTENSION IF NOT EXISTS citext; CREATE EXTENSION IF NOT EXISTS postgis;
                    CREATE EXTENSION IF NOT EXISTS pg_trgm; CREATE EXTENSION IF NOT EXISTS pgcrypto;"
}

drop_db() {
  psql_db postgres -c "DROP DATABASE IF EXISTS \"$1\" WITH (FORCE)"
}
