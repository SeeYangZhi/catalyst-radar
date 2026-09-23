# Example deployment: single GCE VM + Docker Compose + Cloudflare Tunnel

This is one worked example of running Catalyst Radar in production. Nothing in
the app is GCP- or Cloudflare-specific: any Linux host with Docker Compose and a
public HTTPS endpoint works. Placeholders used below:

| Placeholder | Meaning |
|---|---|
| `<your-project>` | GCP project id |
| `<zone>` | Compute Engine zone, e.g. `us-central1-a` |
| `catalyst-radar` | VM name (pick your own) |
| `radar.example.com` | public hostname routed through the tunnel |
| `<your-backup-bucket>` | GCS bucket for off-site backups |

The whole stack runs under Docker Compose (`docker-compose.prod.yml`): api,
worker, beat, postgres, redis, the Next.js frontend, and nginx in front. A
Cloudflare Tunnel (`cloudflared`, a systemd service on the VM) provides the
public HTTPS endpoint that Telegram's webhook requires; the dashboard uses the
same tunnel.

```
Telegram ──► Cloudflare edge ──► cloudflared (systemd) ──► nginx:8081 ──► api:8000
Browser  ──► Cloudflare edge ──► cloudflared (systemd) ──► nginx:8081 ──┤
                                                                        └► frontend:3000
                                       postgres / redis / worker / beat (internal compose net)
```

nginx is the single origin: it routes `/api/v1/*` to `api:8000` and everything
else to `frontend:3000`, so the tunnel needs only one public hostname.

| File | Purpose |
|---|---|
| `docker-compose.prod.yml` | Hardened stack (no exposed DB ports, restart policies, Redis AOF, persisted beat schedule) |
| `deploy/deploy.sh` | Ship HEAD to the VM, prune deleted files, rebuild, verify |
| `deploy/verify.sh` | On-host health check (containers, deployed commit, health endpoint, Telegram webhook) |
| `deploy/backup/pg_dump_backup.sh` | Nightly Postgres dump, local pruning, optional GCS upload |
| `deploy/backup/restore_test.sh` | Restore the newest dump into a throwaway container and assert it is usable |
| `deploy/backup/gcs-lifecycle.json` | 30-day delete rule for the backup bucket |

Sizing that works: `e2-medium` (2 vCPU, 4 GB) with 4 GB swap, Debian 12,
40 GB balanced persistent disk.

---

## 0. Prerequisites (workstation)

- `gcloud` CLI authenticated (`gcloud auth login`), a GCP project with billing.
- A domain on Cloudflare (free plan is fine) for the tunnel.

```bash
gcloud config set project <your-project>
gcloud services enable compute.googleapis.com
```

## 1. Provision the VM

```bash
gcloud compute instances create catalyst-radar \
  --zone=<zone> \
  --machine-type=e2-medium \
  --image-family=debian-12 --image-project=debian-cloud \
  --boot-disk-size=40GB --boot-disk-type=pd-balanced \
  --no-service-account --no-scopes
```

`--no-service-account --no-scopes`: the VM needs no GCP API access (least
privilege). No HTTP/HTTPS firewall rules are opened: `cloudflared` dials out to
Cloudflare, so there is no inbound public port.

### SSH through IAP

```bash
# One-time: allow IAP's range to reach SSH.
gcloud compute firewall-rules create allow-iap-ssh \
  --direction=INGRESS --action=ALLOW --rules=tcp:22 \
  --source-ranges=35.235.240.0/20

gcloud compute ssh catalyst-radar --zone=<zone> --tunnel-through-iap
```

Use `--tunnel-through-iap` for every SSH / scp. Repeated SSH to the external IP
can get rate-limited (`kex_exchange_identification: Connection closed`).

## 2. Install Docker + swap

The image builds (headless Chromium for the HK IPO scraper, the Next.js build)
need swap headroom on 4 GB RAM.

```bash
gcloud compute ssh catalyst-radar --zone=<zone> --tunnel-through-iap --command='
  sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile
  sudo mkswap /swapfile && sudo swapon /swapfile
  echo "/swapfile none swap sw 0 0" | sudo tee -a /etc/fstab

  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker $USER          # applies on next SSH session
  sudo systemctl enable --now docker     # stack returns on every boot
'
```

## 3. Get the code onto the VM

Ship the tracked tree (no secrets, no local caches) over IAP; no Git
credentials are needed on the VM:

```bash
git archive --format=tar HEAD | \
gcloud compute ssh catalyst-radar --zone=<zone> --tunnel-through-iap \
  --command='mkdir -p ~/catalyst-radar && tar -x -C ~/catalyst-radar'
```

(Cloning the public repo on the VM also works; `deploy.sh` below assumes the
`git archive` flow.)

## 4. Production env (`backend/.env` on the VM)

Keep the VM's `backend/.env` as the production config. Start from
`backend/.env.example` and set at least:

```ini
ENVIRONMENT=production
DEBUG=false

# Rotate all of these. The app refuses to start in production with the
# default JWT secret or admin password.
JWT_SECRET_KEY=<openssl rand -hex 32>
DEFAULT_ADMIN_PASSWORD=<strong unique password>
POSTGRES_PASSWORD=<strong unique password>

CORS_ORIGINS=https://radar.example.com

TELEGRAM_BOT_TOKEN=<from @BotFather>
TELEGRAM_ADMIN_CHAT_ID=<your chat id>
TELEGRAM_WEBHOOK_URL=https://radar.example.com/api/v1/telegram/webhook
TELEGRAM_WEBHOOK_SECRET=<openssl rand -hex 24>

EODHD_API_KEY=...
OPENAI_API_KEY=...
```

Notes:

- `POSTGRES_PASSWORD` is only read by Postgres when the volume is first
  initialized. Set it before the first `up`; changing it later requires
  `ALTER USER` inside the database.
- The prod compose injects `DATABASE_URL` / `REDIS_URL` / Celery URLs itself
  (pointing at the `postgres` / `redis` services), overriding `.env`.
- The frontend reads `NEXT_PUBLIC_*` from the committed
  `frontend/.env.production` at build time. It uses a relative `/api/v1`, so
  nothing changes per hostname.
- Webhook vs polling is governed by `TELEGRAM_WEBHOOK_URL`: when set, the api
  registers the webhook on startup and polling is skipped.

## 5. Cloudflare Tunnel

1. Cloudflare Zero Trust ▸ Networks ▸ Tunnels ▸ Create a tunnel ▸
   Cloudflared. Copy the tunnel token.
2. Under the tunnel's Public Hostnames add `radar.example.com` with service
   HTTP → `http://localhost:8081` (nginx, which serves both the dashboard and
   `/api/v1/`, including the Telegram webhook).
3. Install it as a service on the VM (token-based, so ingress is configured in
   the Cloudflare dashboard, not a local file):

```bash
gcloud compute ssh catalyst-radar --zone=<zone> --tunnel-through-iap --command='
  curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb -o /tmp/cloudflared.deb
  sudo dpkg -i /tmp/cloudflared.deb
  sudo cloudflared service install <TUNNEL_TOKEN>
  systemctl is-active cloudflared
'
```

Consider putting the dashboard hostname behind Cloudflare Access as an extra
layer; if you do, exempt `/api/v1/telegram/webhook` so Telegram can reach it.

## 6. Launch

`docker compose` interpolates `${POSTGRES_PASSWORD}` from a project-level env
file, so always pass `--env-file backend/.env` (the per-service `env_file:` only
injects into containers):

```bash
gcloud compute ssh catalyst-radar --zone=<zone> --tunnel-through-iap --command='
  cd ~/catalyst-radar
  docker compose --env-file backend/.env -f docker-compose.prod.yml up -d --build
  docker compose --env-file backend/.env -f docker-compose.prod.yml ps
'
```

The `api` container runs `alembic upgrade head` on start. The first build is
slow (Chromium + Next.js); later code-only changes reuse cached layers.

## 7. Verify

```bash
curl -fsS https://radar.example.com/api/v1/health
curl -fsS -o /dev/null -w '%{http_code}\n' https://radar.example.com/   # 200
```

On the VM, `bash ~/catalyst-radar/deploy/verify.sh` checks container state, the
local health endpoint, and the Telegram webhook registration. Then `/start` the
bot from your Telegram account and log in to the dashboard with
`DEFAULT_ADMIN_EMAIL` / `DEFAULT_ADMIN_PASSWORD`.

## 8. Updating

`deploy/deploy.sh` ships the committed tree (HEAD), prunes stale files, rebuilds,
and verifies before declaring success. It needs four env vars and refuses to run
without them:

```bash
export PROJECT=<your-project> ZONE=<zone> VM=catalyst-radar \
       PUBLIC_URL=https://radar.example.com
./deploy/deploy.sh                 # whole stack
./deploy/deploy.sh api worker beat # only these services
```

`REMOTE_DIR` (default `$HOME/catalyst-radar` on the VM) is optional.

Things the script handles that a manual deploy must too:

- **Deleted / renamed files.** `tar -x` overlays but never deletes, so a file
  removed or renamed in git would linger on the VM and be baked into the next
  build (a renamed Alembic migration leaves two heads and the api crash-loops).
  The script keeps a manifest of shipped tracked files (`.deployed_manifest`)
  and deletes any path that dropped out of HEAD. Untracked runtime files
  (`backend/.env`, `.deployed_sha`) are never touched.
- **Separate images.** api / worker / beat share the `backend/` build context
  but are separate images. Rebuild them together or one is left stale.
- **Committed tree only.** It deploys `git archive HEAD` and warns when the
  working tree is dirty.

After editing only `backend/.env`, recreate the affected service:
`docker compose --env-file backend/.env -f docker-compose.prod.yml up -d --force-recreate api`.

### Disk space

Repeated rebuilds accumulate Docker build cache and dangling images; a 40 GB
disk fills up over time. Before a rebuild on a tight disk:

```bash
df -h /
docker builder prune -af
docker image prune -f
```

## 9. Backups

The `pgdata` / `redisdata` volumes live on the VM's disk. Use logical dumps,
not disk snapshots of live volumes. `deploy/backup/pg_dump_backup.sh` dumps
`radar_db` to `~/CatalystRadarBackups`, prunes local copies older than
`RETAIN_DAYS` (14), and uploads each dump to Cloud Storage when `GCS_BUCKET` is
set.

```bash
~/catalyst-radar/deploy/backup/pg_dump_backup.sh    # test once (local only)

# crontab -e on the VM — nightly at 03:30:
30 3 * * * GCS_BUCKET=<your-backup-bucket> /home/<user>/catalyst-radar/deploy/backup/pg_dump_backup.sh >> /home/<user>/catalyst-radar-backup.log 2>&1
```

### Off-site to Google Cloud Storage (one-time)

The VM has no attached service account, so a dedicated backup service account
with a key file, scoped to one bucket, authenticates the nightly upload.

```bash
PROJECT=<your-project>
BUCKET=<your-backup-bucket>                  # bare name, globally unique
SA_NAME=<backup-sa-name>                      # e.g. catalyst-backup
SA=$SA_NAME@$PROJECT.iam.gserviceaccount.com

gcloud storage buckets create gs://$BUCKET \
  --project=$PROJECT --location=<region> \
  --uniform-bucket-level-access --public-access-prevention
gcloud storage buckets update gs://$BUCKET \
  --lifecycle-file=deploy/backup/gcs-lifecycle.json

gcloud iam service-accounts create $SA_NAME \
  --project=$PROJECT --display-name="Catalyst Radar nightly backup"
gcloud storage buckets add-iam-policy-binding gs://$BUCKET \
  --member="serviceAccount:$SA" --role=roles/storage.objectAdmin

gcloud iam service-accounts keys create /tmp/backup-key.json --iam-account=$SA
gcloud compute scp /tmp/backup-key.json catalyst-radar:~/backup-key.json \
  --zone=<zone> --tunnel-through-iap
rm /tmp/backup-key.json
# On the VM:
chmod 600 ~/backup-key.json
gcloud auth activate-service-account --key-file=$HOME/backup-key.json
```

`GCS_PREFIX` (default `catalyst-radar`) is the in-bucket folder. Remote pruning
is the bucket lifecycle rule (30-day delete), not the script.

### Restore

```bash
gunzip -c ~/CatalystRadarBackups/radar_db-YYYYMMDD-HHMMSS.sql.gz \
  | docker compose --env-file backend/.env -f docker-compose.prod.yml exec -T postgres \
    psql -U radar_user -d radar_db
```

### Prove a backup is recoverable

`deploy/backup/restore_test.sh` restores the newest dump into a disposable
Postgres container (never the production one) and asserts the core tables came
back with rows. Run it after setup and periodically:

```bash
~/catalyst-radar/deploy/backup/restore_test.sh                                     # newest local dump
GCS_BUCKET=<your-backup-bucket> ~/catalyst-radar/deploy/backup/restore_test.sh --from-gcs
```

## Operating notes

- **Auto-recovery.** Every long-lived service is `restart: unless-stopped`,
  Docker starts on boot, swap is in `fstab`, and `cloudflared` is an enabled
  systemd service, so the stack returns after a reboot with no intervention.
- **Beat schedule persists** on the `beatdata` volume, so frequent deploys do
  not reset interval timers; an overdue task fires on boot.
- **Logs:** `docker compose --env-file backend/.env -f docker-compose.prod.yml logs -f worker beat`.
- **Ingestion health:** the dashboard's Source Runs page, plus the self-monitoring
  watchdog that pings admin chats on stalls. To re-run a sync manually:
  `docker compose ... exec worker uv run celery -A catalyst_radar.celery_app:celery_app call catalyst_radar.sync_ipos`.
- **Database access:** Postgres / Redis have no host ports.
  `docker compose --env-file backend/.env -f docker-compose.prod.yml exec postgres psql -U radar_user -d radar_db`.
- **Rotating a secret:** edit `backend/.env`, then `up -d --force-recreate <service>`.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `POSTGRES_PASSWORD is missing` / `set in backend/.env` on compose | forgot `--env-file backend/.env` | add it to every `docker compose` command |
| api: `password authentication failed` | `POSTGRES_PASSWORD` changed after the volume was initialized | restore the original password or `ALTER USER radar_user PASSWORD ...` |
| api crash-loops with multiple Alembic heads | stale migration file left on the host | deploy with `deploy.sh` (prunes), or delete the stale file |
| 404 on `/api/v1/*` via the tunnel | public hostname points at the frontend | point the tunnel at `http://localhost:8081` (nginx) |
| `kex_exchange_identification: Connection closed` on SSH | external-IP SSH rate-limited | use `--tunnel-through-iap` |
| Telegram webhook empty in `getWebhookInfo` | api booted before `TELEGRAM_WEBHOOK_URL` was set | `up -d --force-recreate api` |
| CORS errors in the browser | `CORS_ORIGINS` differs from the public hostname | set `CORS_ORIGINS=https://radar.example.com`, recreate api |
| build fails with `no space left on device` | Docker build cache | `docker builder prune -af` |
