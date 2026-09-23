#!/bin/bash
# Nightly Postgres backup for the Catalyst Radar production stack.
#
# Dumps the radar_db database from the running `postgres` container into a
# compressed, timestamped file, prunes local copies older than RETAIN_DAYS, and
# (when GCS_BUCKET is set) uploads the dump off-site to Google Cloud Storage so
# a lost host disk can't take the backups with it.
#
# Schedule it from cron (see deploy/README.md) — e.g. nightly at 03:30.
set -euo pipefail

# --- config -----------------------------------------------------------------
# Derive PROJECT_DIR from the script's own location so the script keeps working
# if the checkout moves.
_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${PROJECT_DIR:-$(cd "$_SCRIPT_DIR/../.." && pwd)}"
COMPOSE_FILE="${COMPOSE_FILE:-$PROJECT_DIR/docker-compose.prod.yml}"
# compose interpolates ${POSTGRES_PASSWORD} from this file (same as every other
# compose invocation in this repo); without it `config` fails before exec runs.
ENV_FILE="${ENV_FILE:-$PROJECT_DIR/backend/.env}"
BACKUP_DIR="${BACKUP_DIR:-$HOME/CatalystRadarBackups}"
RETAIN_DAYS="${RETAIN_DAYS:-14}"
DB_USER="${DB_USER:-radar_user}"
DB_NAME="${DB_NAME:-radar_db}"
# Off-site copy. Leave GCS_BUCKET empty for local-only backups.
# GCS_BUCKET is a bare bucket name (no gs://). Objects land under GCS_PREFIX.
GCS_BUCKET="${GCS_BUCKET:-}"
GCS_PREFIX="${GCS_PREFIX:-catalyst-radar}"
# ---------------------------------------------------------------------------

# cron runs with a minimal PATH; make the docker/gcloud locations explicit
# (Debian installs the engine and the Cloud SDK under /usr/bin).
export PATH="/usr/local/bin:/usr/bin:/bin"

mkdir -p "$BACKUP_DIR"
ts="$(date +%Y%m%d-%H%M%S)"
out="$BACKUP_DIR/radar_db-$ts.sql.gz"

echo "[$(date -u +%FT%TZ)] dumping $DB_NAME -> $out"
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T postgres \
  pg_dump -U "$DB_USER" -d "$DB_NAME" --clean --if-exists \
  | gzip > "$out"

# Fail loudly if the dump is suspiciously small (e.g. container was down).
size=$(stat -c%s "$out")
if [ "$size" -lt 1024 ]; then
  echo "ERROR: dump is only ${size} bytes — treating as failed" >&2
  rm -f "$out"
  exit 1
fi
echo "[$(date -u +%FT%TZ)] local ok (${size} bytes)"

# Off-site upload. The dump is already safely on local disk, so an upload
# failure must NOT delete it — but it should still exit non-zero so cron's log
# (and the operator) sees that off-site protection lapsed for this run.
if [ -n "$GCS_BUCKET" ]; then
  dest="gs://$GCS_BUCKET/$GCS_PREFIX/radar_db-$ts.sql.gz"
  echo "[$(date -u +%FT%TZ)] uploading -> $dest"
  if command -v gcloud >/dev/null 2>&1; then
    gcloud storage cp "$out" "$dest"
  else
    gsutil cp "$out" "$dest"
  fi
  echo "[$(date -u +%FT%TZ)] upload ok"
  # Remote pruning is handled by the bucket lifecycle rule (deploy/backup/
  # gcs-lifecycle.json), not here — one less permission the backup SA needs.
fi

echo "[$(date -u +%FT%TZ)] pruning local copies > ${RETAIN_DAYS}d"
find "$BACKUP_DIR" -name 'radar_db-*.sql.gz' -mtime "+${RETAIN_DAYS}" -delete

echo "[$(date -u +%FT%TZ)] done"
