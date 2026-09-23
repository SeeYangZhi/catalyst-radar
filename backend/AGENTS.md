# backend/ — Python service

## Purpose

FastAPI + Celery + Postgres backend for Catalyst Radar. Owns: HTTP API, beat scheduler, async workers, persistence, source-adapter ingestion, alert generation + delivery, and the test suite.

## Ownership

Everything under `backend/`. Frontend is owned by `frontend/AGENTS.md`. Compose files and nginx config live at the repo root and are owned by the root contract.

## Local Contracts

### Layout

- `catalyst_radar/` — application package (see child docs)
  - `adapters/` — external data-source adapters; see `catalyst_radar/adapters/AGENTS.md`
  - `services/` — business logic; see `catalyst_radar/services/AGENTS.md`
  - `api/v1/` — FastAPI routers; see `catalyst_radar/api/v1/AGENTS.md`
  - `models/` — SQLModel tables (one file per domain, mirrors `repositories/`)
  - `repositories/` — DB access classes returning ORM objects, NEVER raw SQL to API routes
  - `schemas/` — pydantic response/request models; LLM Structured Outputs schemas under `schemas/llm.py`
  - `tasks.py` — Celery task wrappers (thin: `_run(job)` + chain sync→enrich→dispatch)
  - `celery_app.py` — beat schedule + boot-time `_boot_override` for runtime-config'd cadences
  - `config.py` — Pydantic Settings (env-driven defaults). Runtime overrides via `runtime_config.effective(session)`
  - `runtime_config.py` — DB-backed overrides for user-editable settings; `EDITABLE` map = source of truth for what the UI can set
  - `db.py` — `async_session_factory`; never import the module-global engine inside a Celery task (use `_run`)
  - `dedup.py` — `source_event_id()`, `dedup_key()`, `content_dedup_key()`, `normalize_url()`
  - `net_guard.py` — SSRF guard: every fetch of a source- or LLM-supplied URL (URL-liveness probes, PDF/xlsx downloads) must go through it; private / loopback / link-local targets are refused
- `alembic/` — schema migrations (one revision per schema change; no out-of-band SQL)
- `tests/` — pytest async suite; see `tests/AGENTS.md`
- `pyproject.toml` — `uv`-managed deps. Add new deps via `uv add <pkg>`, never edit by hand
- `Dockerfile` — single image shared by api/worker/beat (the compose service tag is the differentiator)

### Cross-cutting rules

- **Async everywhere at runtime**. `AsyncSession`, `httpx.AsyncClient`, `asyncio.to_thread` for blocking libs (akshare / tushare / pandas).
- **Idempotency**. Every sync writes via `EventRepository.upsert()` keyed on `source_event_id`; cross-source URL dedup via `dedup_key` (URL-canonicalized, see `dedup.normalize_url`). Payload refreshes merge-by-default: incoming keys overwrite (adapters emit their full key set, explicit `None` included), keys absent from the incoming payload survive — so enrichment/lifecycle/user-written payload keys are never clobbered by a resync. A source that needs a key cleared must emit it explicitly, not omit it.
- **Raw payload first**. `RawItemRepository.store()` before normalization — gives a debuggable audit trail when an adapter ships a regression.
- **Source runs**. Wrap every external call with `SourceRunRepository.start()` / `.finish(status=..., last_error=...)`. The UI surfaces these to diagnose ingest health. `.finish(summary={...})` persists per-run structured counters on the run's JSON `summary` column (e.g. `{"url_probes": {kept, dropped, kept_non_2xx, dropped_status}}` for the websearch URL-liveness probes) — durable telemetry that survives deploys, unlike container logs. Omit the key entirely when the counted thing didn't run.
- **Best-effort outer try**. One bad row / one bad upstream must NEVER abort a sync. `except Exception: ... continue` with structured-log warning is the pattern; let the next item in the loop proceed.
- **Settings vs runtime_config**. Cadences and feature toggles that the user edits live in `runtime_config.EDITABLE`; read via `await effective(session)`. Cadences read by Celery Beat at startup belong in `RESTART_REQUIRED`. Pure env-only knobs (API keys, thresholds) stay in `config.Settings`.
- **Strict-mode LLM output**. Every classifier/extractor call uses a Pydantic model from `schemas/llm.py` passed as `text_format=...`. No hand-rolled JSON schemas. `additionalProperties: false` enforced via the `_StrictModel` base.

### Logging

`structlog` only. `log.info(event="...", **kwargs)` — `event` is a stable enum-like string per call site (used for log-aggregation queries). No f-string log messages.

### Process boundaries

- **API** serves HTTP; never starts long jobs synchronously. Enqueue via `.delay()`.
- **Worker** runs Celery tasks. Each task uses `_run(job)` to spin up a fresh async engine per loop (asyncpg + Celery's prefork pool would otherwise reuse engines across loops → "attached to a different loop" errors).
- **Beat** schedules tasks. Beat slots in `celery_app.py:beat_schedule`. Cadences read via `_boot_override` so the UI can change them without an env edit.

## Work Guidance

- Before adding a Celery task: add the function in `tasks.py`, the beat slot in `celery_app.py`, and (if cadence is user-tunable) the runtime-config key + `_boot_override` lookup. Bump the registered-task list in any inspection scripts only if relied on.
- Before adding a new external source: read `catalyst_radar/adapters/AGENTS.md` for the SourceAdapter contract.
- Before adding a new alert formatter: see `catalyst_radar/services/AGENTS.md` (alerts re-render on dispatch for IPO events; respect the `payload["stage"]`-style discriminator).
- Before adding a new API endpoint: see `catalyst_radar/api/v1/AGENTS.md` (CurrentUser / SessionDep, repository pattern, response_model required).
- New deps: `uv add <pkg>` then commit both `pyproject.toml` and `uv.lock`. Same lock-file rule for `uv add --dev`.

## Verification

- `cd backend && uv run pytest -q` — full suite. Must be green before commit.
- `uv run ruff check .` — lint (CI runs it on the whole backend).
- For Docker changes: rebuild ALL affected services (api, worker, beat are separate images sharing the same Dockerfile). `docker compose -f docker-compose.prod.yml build api worker beat` then `up -d --force-recreate`.

## Child DOX Index

- [`catalyst_radar/adapters/AGENTS.md`](catalyst_radar/adapters/AGENTS.md) — source-adapter contract (SourceAdapter base, source_event_id / dedup_key, raw-payload-first).
- [`catalyst_radar/services/AGENTS.md`](catalyst_radar/services/AGENTS.md) — sync / enrich / dispatch patterns, alert formatters, dedup layers.
- [`catalyst_radar/api/v1/AGENTS.md`](catalyst_radar/api/v1/AGENTS.md) — FastAPI router conventions (deps, response_model, fire-and-forget enqueue).
- [`tests/AGENTS.md`](tests/AGENTS.md) — pytest async, db_session fixture, stub patterns.
