#!/usr/bin/env bash
# Post-deploy / health verification — runs ON THE VM.
#
# Hard-fails (exit 1) if the stack isn't actually serving. deploy.sh runs this
# right after `up -d --build`, but it's also runnable standalone to answer
# "is the deployment healthy right now?":
#
#   bash ~/catalyst-radar/deploy/verify.sh
#   EXPECT_SHA=<sha> bash ~/catalyst-radar/deploy/verify.sh   # also assert running code
set -uo pipefail

PROJECT_DIR="${PROJECT_DIR:-$HOME/catalyst-radar}"
COMPOSE_FILE="${COMPOSE_FILE:-$PROJECT_DIR/docker-compose.prod.yml}"
ENV_FILE="${ENV_FILE:-$PROJECT_DIR/backend/.env}"
# Expected deployed commit (optional). Falls back to the first positional arg.
EXPECT_SHA="${EXPECT_SHA:-${1:-}}"
# nginx is the single in-VM origin (serves both the API and the dashboard).
HEALTH_URL="${HEALTH_URL:-http://localhost:8081/api/v1/health}"
HEALTH_RETRIES="${HEALTH_RETRIES:-30}"   # api restarts + runs migrations on boot
HEALTH_DELAY="${HEALTH_DELAY:-3}"

export PATH="/usr/local/bin:/usr/bin:/bin"
cd "$PROJECT_DIR" || { echo "FATAL: $PROJECT_DIR not found" >&2; exit 2; }
COMPOSE="docker compose --env-file $ENV_FILE -f $COMPOSE_FILE"

fail=0
ok()   { echo "  ok    $*"; }
bad()  { echo "  FAIL  $*" >&2; fail=1; }
warn() { echo "  WARN  $*"; }

echo "== container state =="
services=$($COMPOSE config --services 2>/dev/null)
# -a so a crashed/exited service is visible, not silently absent.
ps_out=$($COMPOSE ps -a --format '{{.Service}} {{.State}} {{.Health}}' 2>/dev/null)
for s in $services; do
  line=$(echo "$ps_out" | awk -v s="$s" '$1==s {print; exit}')
  state=$(echo "$line" | awk '{print $2}')
  health=$(echo "$line" | awk '{print $3}')
  if [ "$state" = "running" ]; then
    if [ "$health" = "unhealthy" ]; then bad "$s: running but unhealthy"
    else ok "$s: running${health:+ ($health)}"; fi
  else
    bad "$s: state=${state:-absent}"
  fi
done

echo "== deployed code =="
deployed=$(cat "$PROJECT_DIR/.deployed_sha" 2>/dev/null || echo "")
if [ -n "$EXPECT_SHA" ]; then
  if [ "$deployed" = "$EXPECT_SHA" ]; then ok "running ${deployed:0:12}"
  else bad "running ${deployed:-none} != expected ${EXPECT_SHA:0:12}"; fi
else
  ok "running ${deployed:-unknown}"
fi

echo "== app health (local nginx) =="
body=""
hok=0
for _ in $(seq 1 "$HEALTH_RETRIES"); do
  if body=$(curl -fsS "$HEALTH_URL" 2>/dev/null); then hok=1; break; fi
  sleep "$HEALTH_DELAY"
done
[ "$hok" = 1 ] && ok "health: $body" || bad "health endpoint never came up ($HEALTH_URL)"

echo "== telegram webhook =="
TOKEN=$(grep -E '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- | tr -d '"' | tr -d "'")
WEBHOOK_URL=$(grep -E '^TELEGRAM_WEBHOOK_URL=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- | tr -d '"' | tr -d "'")
if [ -z "$TOKEN" ]; then
  warn "no TELEGRAM_BOT_TOKEN in env — skipping"
elif [ -z "$WEBHOOK_URL" ]; then
  ok "TELEGRAM_WEBHOOK_URL unset — bot runs in polling mode, skipping webhook check"
else
  info=$(curl -s "https://api.telegram.org/bot${TOKEN}/getWebhookInfo" 2>/dev/null)
  url=$(echo "$info" | grep -oE '"url":"[^"]*"' | head -1 | cut -d'"' -f4)
  lasterr=$(echo "$info" | grep -oE '"last_error_message":"[^"]*"' | head -1 | cut -d'"' -f4)
  [ -n "$url" ] && ok "webhook registered: $url" || bad "webhook url is empty"
  # last_error can be stale (pre-deploy), so warn rather than fail.
  [ -n "$lasterr" ] && warn "webhook last_error: $lasterr"
fi

if [ "$fail" = 0 ]; then echo "VERIFY OK"; exit 0; fi
echo "VERIFY FAILED" >&2
exit 1
