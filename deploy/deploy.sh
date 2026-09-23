#!/usr/bin/env bash
# One-command deploy + post-deploy verification for Catalyst Radar.
#
# Example deploy for the single-GCE-VM setup described in deploy/README.md.
# Run from your workstation (needs an authenticated gcloud). Ships the
# committed tree (HEAD) to the VM over IAP, rebuilds the stack, then verifies —
# on the VM and end-to-end through the public URL — before declaring success.
# Exits non-zero with a rollback hint if anything fails to come up.
#
# Required env vars (no defaults — they describe YOUR deployment):
#   PROJECT     GCP project id              e.g. my-radar-project
#   ZONE        Compute Engine zone         e.g. us-central1-a
#   VM          instance name               e.g. catalyst-radar
#   PUBLIC_URL  public origin of the stack  e.g. https://radar.example.com
# Optional:
#   REMOTE_DIR  checkout dir on the VM (default: $HOME/catalyst-radar)
#
#   PROJECT=... ZONE=... VM=... PUBLIC_URL=... ./deploy/deploy.sh                 # full stack
#   PROJECT=... ZONE=... VM=... PUBLIC_URL=... ./deploy/deploy.sh api worker beat # only these services
#
# Tip: keep the four `export` lines in a gitignored file (e.g. deploy/.env.deploy) and
# `source` it before running.
set -euo pipefail

missing=()
for var in PROJECT ZONE VM PUBLIC_URL; do
  [ -n "${!var:-}" ] || missing+=("$var")
done
if [ "${#missing[@]}" -gt 0 ]; then
  echo "ERROR: required env var(s) not set: ${missing[*]}" >&2
  echo "Example: PROJECT=my-project ZONE=us-central1-a VM=catalyst-radar \\" >&2
  echo "         PUBLIC_URL=https://radar.example.com ./deploy/deploy.sh" >&2
  exit 2
fi
PUBLIC_URL="${PUBLIC_URL%/}"
# Default keeps a literal $HOME so the VM's shell expands it, not yours.
REMOTE_DIR="${REMOTE_DIR:-\$HOME/catalyst-radar}"
COMPOSE='docker compose --env-file backend/.env -f docker-compose.prod.yml'
SERVICES="$*"   # optional service list; empty = whole stack

ssh_vm() {
  gcloud compute ssh "$VM" --zone="$ZONE" --project="$PROJECT" \
    --tunnel-through-iap --command="$1"
}

# --- preflight --------------------------------------------------------------
command -v gcloud >/dev/null || { echo "gcloud not found on PATH" >&2; exit 1; }
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || { echo "not a git repo" >&2; exit 1; }
SHA=$(git rev-parse HEAD)
SHORT=$(git rev-parse --short HEAD)
BRANCH=$(git rev-parse --abbrev-ref HEAD)
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "WARNING: working tree is dirty — uncommitted changes are NOT deployed"
  echo "         (git archive ships HEAD only). Commit first if you meant to."
fi
echo "==> deploying $BRANCH @ $SHORT → $VM ${SERVICES:+(services: $SERVICES)}"

# --- 1. ship the tracked tree ----------------------------------------------
# `tar -x` overlays files but never deletes, so a file removed or RENAMED in
# git would linger on the VM and get baked into the next build (a renamed
# Alembic migration can leave two heads → api crash-loop). We carry a manifest
# of the tracked files we shipped last time and delete any path that has since
# dropped out of HEAD. Untracked runtime files (backend/.env, .deployed_sha,
# the manifest itself) are never in the manifest, so they are never touched.
echo "==> shipping tree (HEAD) over IAP"
git archive --format=tar HEAD | \
  ssh_vm "tar -x -C $REMOTE_DIR && printf '%s\n' '$SHA' > $REMOTE_DIR/.deployed_sha && echo '    shipped'"

echo "==> pruning files no longer tracked in HEAD"
git -c core.quotePath=false ls-tree -r --name-only HEAD | \
  ssh_vm "cd $REMOTE_DIR && cat > .deployed_manifest.new && \
    if [ -f .deployed_manifest ]; then \
      grep -vxF -f .deployed_manifest.new .deployed_manifest > .deployed_manifest.removed || true; \
      n=0; while IFS= read -r f; do [ -n \"\$f\" ] && rm -f -- \"\$f\" && n=\$((n+1)); done < .deployed_manifest.removed; \
      rm -f .deployed_manifest.removed; \
      echo \"    pruned \$n stale file(s)\"; \
    else echo '    no prior manifest — nothing to prune'; fi && \
    mv .deployed_manifest.new .deployed_manifest"

# --- 2. build + (re)start ---------------------------------------------------
echo "==> building & starting (this is slow on first build)"
ssh_vm "cd $REMOTE_DIR && $COMPOSE up -d --build $SERVICES"

# --- 3. verify on the VM ----------------------------------------------------
echo "==> verifying on the VM"
ssh_vm "EXPECT_SHA='$SHA' bash $REMOTE_DIR/deploy/verify.sh"

# --- 4. verify end-to-end through the public tunnel -------------------------
echo "==> verifying public endpoints ($PUBLIC_URL)"
rc=0
code=$(curl -fsS -o /dev/null -w '%{http_code}' "$PUBLIC_URL/api/v1/health" 2>/dev/null || echo 000)
[ "$code" = 200 ] && echo "  ok    public health: $code" || { echo "  FAIL  public health: $code" >&2; rc=1; }
code=$(curl -fsS -o /dev/null -w '%{http_code}' "$PUBLIC_URL/" 2>/dev/null || echo 000)
[ "$code" = 200 ] && echo "  ok    dashboard: $code" || { echo "  FAIL  dashboard: $code" >&2; rc=1; }

if [ "$rc" = 0 ]; then
  echo "DEPLOY OK — $BRANCH @ $SHORT is live"
  exit 0
fi
echo "DEPLOY VERIFICATION FAILED" >&2
echo "The new images are built but not serving. To roll back, deploy a known-good" >&2
echo "commit:  git checkout <good-sha> && ./deploy/deploy.sh   (then return to your branch)" >&2
exit 1
