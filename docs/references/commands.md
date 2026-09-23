# Commands Reference

## Docker (full stack)

`docker compose` interpolates `${POSTGRES_PASSWORD}` from a project-level env
file, so always pass `--env-file backend/.env` (the per-service `env_file:`
only injects into containers).

```bash
cp backend/.env.example backend/.env        # then edit
docker compose --env-file backend/.env -f docker-compose.dev.yml up --build
docker compose --env-file backend/.env -f docker-compose.dev.yml logs -f worker beat
docker compose --env-file backend/.env -f docker-compose.dev.yml down
```

Dev stack ports: dashboard `http://localhost:3000`, API `http://localhost:8000`
(`/api/v1/health`, OpenAPI docs at `/docs`), Postgres `5432`, Redis `6379`.

## Backend

```bash
cd backend
uv sync                                        # deps incl. dev group
uv run alembic upgrade head                    # apply migrations
uv run uvicorn catalyst_radar.main:app --host 0.0.0.0 --port 8000 --reload
uv run celery -A catalyst_radar.celery_app:celery_app worker --loglevel=info
uv run celery -A catalyst_radar.celery_app:celery_app beat --loglevel=info
```

Tests and lint:

```bash
cd backend
uv run pytest -q
uv run pytest tests/test_<x>.py -x --tb=short
uv run ruff check .
uv run ruff format .
```

Migrations (revision ids must be 32 characters or fewer; Alembic stores them in
a `varchar(32)` column):

```bash
cd backend
uv run alembic revision --autogenerate -m "describe change"
```

One-off syncs (outside Celery):

```bash
cd backend
uv run python -m catalyst_radar.scripts.sync_company_reference
uv run python -m catalyst_radar.scripts.sync_earnings
uv run python -m catalyst_radar.scripts.sync_ipos
uv run python -m catalyst_radar.scripts.sync_catalysts
```

Trigger a Celery task on a running stack (task names are in
`backend/catalyst_radar/tasks.py`):

```bash
docker compose --env-file backend/.env -f docker-compose.dev.yml exec worker \
  uv run celery -A catalyst_radar.celery_app:celery_app call catalyst_radar.sync_ipos
```

## Frontend

```bash
cd frontend
bun install
bun dev                 # http://localhost:3000, expects the API on :8000
bun run typecheck
bun run lint
bun run test
```

Before any Next.js work, read the docs bundled with the installed version:

```bash
find frontend/node_modules/next/dist/docs -type f | sort | sed -n '1,120p'
```

## Environment files

- `backend/.env` (local, never committed) from `backend/.env.example`.
- `frontend/.env.local` (optional, local dev overrides).
- `frontend/.env.production` (committed): `NEXT_PUBLIC_*` values baked in at
  `next build` time. Relative `/api/v1`, so the production bundle is
  hostname-independent behind nginx.

Keep new backend config variables in both `backend/.env.example` and
`backend/catalyst_radar/config.py`.

## Production

`docker-compose.prod.yml` runs the same services plus nginx, with no host-exposed
database ports and restart policies. An example single-VM deployment with a
Cloudflare Tunnel, deploy script, and backups is in
[`deploy/README.md`](../../deploy/README.md).

```bash
docker compose --env-file backend/.env -f docker-compose.prod.yml up -d --build
```

- api / worker / beat are separate images sharing the `backend/` context:
  rebuild all the relevant services, not just `api`.
- After editing only `backend/.env`, recreate the affected service:
  `... up -d --force-recreate api`.
- With `TELEGRAM_WEBHOOK_URL` set the api registers the Telegram webhook on
  startup; recreate `api` after changing it.
