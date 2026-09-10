#!/usr/bin/env bash
# Build a local PostgreSQL database from the committed migrations.
#
# Useful for two things: running the database test suite without Supabase, and
# checking that the migrations actually apply from scratch — the defect that
# started this work was a schema that only existed on the live project.
#
#   ./scripts/local_db.sh                 # create/refresh cma_dev
#   ./scripts/local_db.sh --db cma_test   # a different database name
set -euo pipefail

DB_NAME="cma_dev"
PGHOST="${PGHOST:-127.0.0.1}"
PGPORT="${PGPORT:-5432}"
PGUSER="${PGUSER:-postgres}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --db) DB_NAME="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

command -v psql >/dev/null || {
  echo "psql not found. Install PostgreSQL 16, or use the Supabase CLI instead." >&2
  exit 1
}

echo "Rebuilding ${DB_NAME} on ${PGHOST}:${PGPORT} ..."
dropdb --if-exists -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" "$DB_NAME"
createdb -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" "$DB_NAME"

run() { psql -q -v ON_ERROR_STOP=1 -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$DB_NAME" -f "$1"; }

# Supabase provides the auth schema and the anon/authenticated/service_role
# roles; a bare PostgreSQL does not, so the shim stands in for them.
run "$ROOT/supabase/tests/00_auth_shim.sql"

for migration in "$ROOT"/supabase/migrations/*.sql; do
  echo "  -> $(basename "$migration")"
  run "$migration"
done

echo
echo "Done. Point the database tests at it with:"
echo "  export CMA_TEST_DATABASE_URL=postgresql://${PGUSER}@${PGHOST}:${PGPORT}/postgres"
echo "  cd backend && uv run pytest tests/db -q"
