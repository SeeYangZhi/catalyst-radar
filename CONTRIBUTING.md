# Contributing to Catalyst Radar

Thanks for helping. Bug reports, source adapters, UI improvements and docs
fixes are all welcome.

## Before you start

- For anything larger than a small fix, open an issue first so we can agree on
  the approach. Unbuilt items are listed under "Roadmap / known gaps" in
  [`docs/architecture.md`](docs/architecture.md).
- Read the `AGENTS.md` chain from the repo root down to the directories you
  touch. They are the per-directory contracts (layout, patterns, verification)
  and apply to human and AI-assisted contributions alike. If your change alters
  a contract, update the nearest `AGENTS.md` in the same PR.
- By contributing you agree that your contributions are licensed under the
  [Apache License 2.0](LICENSE).

## Dev setup

Prerequisites: [uv](https://docs.astral.sh/uv/), [Bun](https://bun.sh),
Docker with Compose.

```bash
cp backend/.env.example backend/.env

# Postgres + Redis
docker compose --env-file backend/.env -f docker-compose.dev.yml up -d postgres redis

# Backend
cd backend
uv sync
uv run alembic upgrade head
uv run uvicorn catalyst_radar.main:app --reload            # API on :8000
uv run celery -A catalyst_radar.celery_app:celery_app worker --loglevel=info
uv run celery -A catalyst_radar.celery_app:celery_app beat --loglevel=info

# Frontend
cd frontend
bun install
bun dev                                                     # dashboard on :3000
```

Or run everything in containers:
`docker compose --env-file backend/.env -f docker-compose.dev.yml up --build`.

Use `uv` for Python and Bun for the frontend (not pip / Poetry / npm / yarn).
Add dependencies with `uv add <pkg>` or `bun add <pkg>` and commit the lock
file.

## Checks (same as CI)

```bash
cd backend
uv run ruff check .
uv run pytest -q

cd frontend
bun run typecheck
bun run lint
bun run test
```

All must pass before a PR is merged.

## Tests

- Tests never touch the network or real DNS. Stub adapters with a subclass
  that returns fixture data; see [`backend/tests/AGENTS.md`](backend/tests/AGENTS.md).
- Fixtures are synthetic data with the exact real-source shape. Never commit
  verbatim third-party captures (HTML pages, API responses, PDFs).
- Bug fixes come with a regression test that fails without the fix.

## Database migrations

- Every schema change is an Alembic migration:
  `cd backend && uv run alembic revision --autogenerate -m "describe change"`.
  Review the generated file; autogenerate misses some changes.
- **Revision ids must be 32 characters or fewer** (Alembic's `version_num`
  column is `varchar(32)`); longer ids fail on PostgreSQL at upgrade time.
- One linear history: rebase onto `main` and re-point `down_revision` if
  another migration landed first. Never edit a migration that is already on
  `main`.

## Adding a data source

Follow [`backend/catalyst_radar/adapters/AGENTS.md`](backend/catalyst_radar/adapters/AGENTS.md):
one adapter per source, raw payload stored before normalization, stable
`source_event_id`, fail-soft `fetch`, a runtime toggle, and a fixture-backed
test. HTTP scrapers of public sites must use `settings.scraper_user_agent`, respect the
site's terms and rate limits, and fetch source-supplied URLs through
`net_guard`.

## Pull requests

- Keep PRs small and focused: one logical change, with tests and doc updates.
- Describe what changed, why, and how you verified it (the PR template has a
  checklist).
- Never commit secrets, `.env` files, personal identifiers (chat ids, domains,
  cloud project ids) or real API responses.
- Match the existing style: `structlog` for logging, repositories for DB access,
  async I/O, shadcn/ui + lucide in the frontend (see [`DESIGN.md`](DESIGN.md)).

## Reporting bugs and security issues

Use the GitHub issue templates for bugs and feature requests. Report security
vulnerabilities privately as described in [`SECURITY.md`](SECURITY.md), not in a
public issue.

## Code of Conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md).
