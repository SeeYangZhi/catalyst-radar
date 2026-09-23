#!/bin/bash
# Restore-test: prove a backup dump is actually recoverable.
#
# A backup you've never restored is a hope, not a backup. This spins up a
# DISPOSABLE postgres container (never touches the production `postgres`
# service or its volume), restores the newest dump into it, and asserts the
# core tables came back with rows. Exits non-zero — loudly — if the restore
# is empty or errors.
#
#   deploy/backup/restore_test.sh                 # newest local dump
#   deploy/backup/restore_test.sh /path/to.sql.gz # a specific dump
#   GCS_BUCKET=my-bucket deploy/backup/restore_test.sh --from-gcs  # newest in GCS
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-$HOME/CatalystRadarBackups}"
GCS_BUCKET="${GCS_BUCKET:-}"
GCS_PREFIX="${GCS_PREFIX:-catalyst-radar}"
DB_NAME="${DB_NAME:-radar_db}"
# The role the production dump grants/owns objects to. Pre-created in the
# throwaway db so the dump's `ALTER ... OWNER TO`/`GRANT` lines apply cleanly
# instead of erroring (harmless, but they bury the real result in noise).
DB_OWNER="${DB_OWNER:-radar_user}"
IMAGE="${IMAGE:-postgres:17-alpine}"
# A throwaway container + password; nothing here is reachable off-box.
CONTAINER="${CONTAINER:-radar-restore-test}"
PGPASSWORD_TEST="restoretest"
# Tables that must exist and be non-empty for the restore to count as good.
REQUIRE_TABLES="${REQUIRE_TABLES:-events source_runs}"

export PATH="/usr/local/bin:/usr/bin:/bin"

# --- pick a dump ------------------------------------------------------------
dump=""
if [ "${1:-}" = "--from-gcs" ]; then
  [ -n "$GCS_BUCKET" ] || { echo "ERROR: --from-gcs needs GCS_BUCKET" >&2; exit 2; }
  latest=$(gcloud storage ls "gs://$GCS_BUCKET/$GCS_PREFIX/radar_db-*.sql.gz" \
    | sort | tail -1)
  [ -n "$latest" ] || { echo "ERROR: no dumps in gs://$GCS_BUCKET/$GCS_PREFIX/" >&2; exit 1; }
  dump=$(mktemp /tmp/radar-restore-XXXX.sql.gz)
  echo "fetching $latest"
  gcloud storage cp "$latest" "$dump"
elif [ -n "${1:-}" ]; then
  dump="$1"
else
  dump=$(ls -1t "$BACKUP_DIR"/radar_db-*.sql.gz 2>/dev/null | head -1 || true)
fi
[ -n "$dump" ] && [ -f "$dump" ] || { echo "ERROR: no dump file found ($dump)" >&2; exit 1; }
echo "restore-testing: $dump ($(stat -c%s "$dump") bytes)"

# --- disposable target ------------------------------------------------------
cleanup() {
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  [ "${1:-}" = "--from-gcs" ] && rm -f "$dump" 2>/dev/null || true
}
trap cleanup EXIT
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
docker run -d --name "$CONTAINER" \
  -e POSTGRES_PASSWORD="$PGPASSWORD_TEST" -e POSTGRES_DB="$DB_NAME" "$IMAGE" >/dev/null

echo "waiting for the throwaway db to accept connections..."
for _ in $(seq 1 30); do
  docker exec "$CONTAINER" pg_isready -U postgres -d "$DB_NAME" >/dev/null 2>&1 && break
  sleep 1
done

# Pre-create the owner role so the dump's ownership/grant statements succeed.
docker exec "$CONTAINER" psql -U postgres -d "$DB_NAME" \
  -c "DO \$\$ BEGIN CREATE ROLE \"$DB_OWNER\" LOGIN; EXCEPTION WHEN duplicate_object THEN NULL; END \$\$;" >/dev/null 2>&1 || true

# --- restore ----------------------------------------------------------------
echo "restoring..."
gunzip -c "$dump" | docker exec -i "$CONTAINER" psql -v ON_ERROR_STOP=0 -U postgres -d "$DB_NAME" >/dev/null

# --- assert -----------------------------------------------------------------
fail=0
for t in $REQUIRE_TABLES; do
  n=$(docker exec "$CONTAINER" psql -tAq -U postgres -d "$DB_NAME" \
        -c "SELECT count(*) FROM \"$t\";" 2>/dev/null || echo "ERR")
  if [ "$n" = "ERR" ] || [ -z "$n" ]; then
    echo "  FAIL  $t: table missing after restore" >&2; fail=1
  elif [ "$n" -eq 0 ]; then
    echo "  WARN  $t: restored but empty (0 rows)"
  else
    echo "  ok    $t: $n rows"
  fi
done

[ "$fail" -eq 0 ] || { echo "RESTORE TEST FAILED" >&2; exit 1; }
echo "RESTORE TEST PASSED"
