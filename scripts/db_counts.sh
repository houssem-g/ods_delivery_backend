#!/usr/bin/env bash
# make db-counts DB=<name>: exact row count of every table of the public schema (and the total).
# Used after a restore to compare with the source database.
source "$(dirname "$0")/db_common.sh"

DB=${1:-${DB:-$MAIN_DB}}
valid_db_name "$DB"
db_exists "$DB" || die "database '$DB' does not exist"

# One UNION ALL query over the application tables (extension tables such as spatial_ref_sys excluded).
query=$(psql_db "$DB" -tA -c "
  SELECT string_agg(format('SELECT %L, count(*) FROM public.%I', c.relname, c.relname),
                    ' UNION ALL ' ORDER BY c.relname)
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
    AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = c.oid AND d.deptype = 'e')")
[[ -n "$query" ]] || die "no tables in '$DB'"
psql_db "$DB" -tA -F $'\t' -c "$query" \
  | awk -F'\t' -v db="$DB" '{ printf "%-32s %10d\n", $1, $2; total += $2 }
                            END { printf "%-32s %10d  (%s)\n", "TOTAL", total, db }'
