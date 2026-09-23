# Catalyst Radar — Root AGENTS.md

Catalyst Radar is a self-hosted, single-admin web app + Telegram bot for financial monitoring. Telegram-first alerting for IPOs, earnings, share-unlocks, and company-specific catalysts across US / HK / CN / TW / KR. Open source under Apache-2.0 (`LICENSE`, `NOTICE`).

## DOX framework

This repo uses the [DOX](https://github.com/agent0ai/dox) AGENTS.md hierarchy. The short version:

- `AGENTS.md` files are binding contracts for their subtree.
- Read the chain from the root down to every path you touch BEFORE editing.
- Closer doc wins on local details; child docs must not weaken root rules.
- Every meaningful change requires a DOX pass — update the closest owning doc and any affected parents.

### Read Before Editing

1. Read this root AGENTS.md.
2. Walk from the repo root to each target path; read every AGENTS.md along the way.
3. Use the nearest AGENTS.md as the local contract; parent docs for repo-wide rules.
4. If docs conflict, the closer doc controls local details. No child doc may weaken the root.

### Update After Editing

Update the closest owning AGENTS.md when a change affects: purpose, scope, ownership; durable structure, contracts, workflows; required inputs/outputs/permissions/constraints; user preferences about behavior, communication, process, or quality; or the AGENTS.md tree itself.

Update parent docs when parent-level structure or child index changes. Remove stale or contradictory text immediately. Small no-contract-change edits may leave docs unchanged but the DOX pass still happens.

### Style

Concise, current, operational. Document stable contracts, not diary entries. Direct bullets with explicit names. Broad rules in parents, concrete details in children. Don't duplicate across files. Delete stale notes instead of explaining history.

### Closeout

Re-check changed paths against the chain → update nearest owning docs and any affected parents/children → refresh affected Child DOX Indexes → remove stale text → run verification when relevant.

## Product North Star

Telegram-first alerting that sends only high-signal market events relevant to the user's configured trading universe.

Scope: US + HK + CN IPO monitoring (including the CN CSRC review stage and pre-IPO tracking); US/HK/KR/TW company reference; earnings via Telegram; company-specific catalysts; one authenticated dashboard admin. KR IPO monitoring, FMP dependency, and automated trading are explicitly out.

- Product spec: `docs/product-specs/PRD-catalyst-radar.md`.
- Architecture + roadmap / known gaps: `docs/architecture.md`.
- Intentional PRD deviations: `docs/decisions.md` (record new ones there).
- Frontend design guide: `DESIGN.md`.
- Commands: `docs/references/commands.md`.
- Contributor-facing docs: `README.md`, `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, `.github/`.

## Tech Stack

| Layer | Tech |
|-------|------|
| Backend | Python 3.13, FastAPI, SQLModel, async SQLAlchemy, Alembic |
| Workers | Celery, Redis |
| Database | PostgreSQL |
| Frontend | Next.js, TypeScript, Tailwind CSS, shadcn/ui |
| Package mgrs | `uv` (Python), Bun (frontend) |
| LLM | OpenAI Responses API (Structured Outputs strict mode) |
| Notifications | Telegram Bot API |
| Sources | EODHD, AAStocks, HKEXnews, MOPS, CNINFO, akshare, tushare, SEC EDGAR, OpenAI web_search |

## Coding Rules (repo-wide)

- `uv` for all Python dep / command execution. Not pip, not Poetry.
- Bun for frontend dep / script execution. Not npm, not yarn.
- `structlog` for backend logging. No `print()` or stdlib logging in app code.
- Repository classes for DB access. No raw SQLAlchemy in API routes.
- `AsyncSession` for all runtime DB access.
- Alembic migrations for schema changes.
- Pydantic Settings for configuration. Runtime overrides via `runtime_config.effective(session)`.
- Secrets only in local `.env`. Never commit real API keys. Keep `.env.example` current.
- Source adapters behind small interfaces. Persist raw source payloads before normalization.
- Event sync is idempotent — stable `source_event_id`, deterministic `dedup_key`.
- Alert generation is SEPARATE from delivery. Notifications retryable and dedup'd.

## Frontend Rules (repo-wide)

- See `frontend/AGENTS.md` — Next.js in this repo has breaking changes vs your training data. Always check `node_modules/next/dist/docs/` before writing Next code.
- shadcn-first for UI primitives. Check the shadcn registry / MCP before writing custom components.
- lucide icons for icon buttons.
- UI cards for repeated items, tables, panels. No nested cards.
- No secrets in frontend code.

## External API Rules

- Check current provider docs before writing against any external API.
- EODHD: response contracts in the PRD; confirm against the OpenAPI spec when in doubt.
- HKEXnews / AAStocks / CNINFO / MOPS: public web sources, not clean JSON APIs. Handle HTML errors + empty results without crashing the sync. HTTP scrapers identify honestly with `settings.scraper_user_agent` (`SCRAPER_USER_AGENT`); don't impersonate a browser UA. Stay polite: throttle, no parallel bursts.
- Outbound fetches of source- or LLM-supplied URLs (URL-liveness probes, document downloads) go through the SSRF guard (`catalyst_radar/net_guard.py`): private / loopback / link-local targets are refused.
- SEC EDGAR: in use for US IPO prospectus enrichment (`edgar_enrich`); requires a descriptive User-Agent and fair-access rate limits.
- MOPS (mops.twse.com.tw): JSON API, ROC calendar dates, link-less items; authoritative TW material-information source.

### News coverage gaps

| Market | EODHD /news | Primary / fallback |
|---|---|---|
| US / HK / KR | ✅ | — |
| CN listed (SSE/SZSE/BSE) | ❌ (not queried) | Eastmoney per-ticker sweep |
| CN pre-IPO (A-codes) | ❌ (no ticker) | web_search sweep |
| Taiwan (TW/TWO/TWSE/TPEX) | ❌ (returns `[]` — verified live) | MOPS material-information sweep (authoritative primary, `catalyst_mops_news_enabled`) + web_search |

For the gap exchanges (`catalyst_websearch_gap_exchanges`, default `TW,TWO,TPEX,SSE,SZSE`)
the web_search sweep runs **even when the master `catalyst_websearch_enabled`
switch is off** — gated by `catalyst_websearch_gap_always_on` (on by default).
The expensive all-markets sweep stays opt-in behind the master switch. Taiwan
listings additionally get the MOPS sweep (`adapters/mops_announcements.py`,
on by default) — TWSE/TPEx companies are legally required to disclose material
information on MOPS, so TW names have a real primary source. The
dashboard's "no news feed" badge (Search + Tracked Companies) only appears
when the MOPS sweep is disabled.

## Operations

- Prod uses `docker-compose.prod.yml`; dev uses `docker-compose.dev.yml`. Always pass `--env-file backend/.env` to `docker compose` — it interpolates `${POSTGRES_PASSWORD}` from a project-level env file (the per-service `env_file:` only injects into containers).
- `deploy/` holds an example single-VM deployment (GCE + Cloudflare Tunnel): `deploy/README.md`, `deploy.sh` (requires `PROJECT`, `ZONE`, `VM`, `PUBLIC_URL`; never hard-code a real deployment's identifiers), `verify.sh`, `backup/`. `deploy.sh` prunes files dropped from HEAD via a shipped-file manifest — keep that when changing it.
- Postgres user `radar_user`, db `radar_db` (compose service `postgres`).
- Default admin: `admin@radar.local`, password from `DEFAULT_ADMIN_PASSWORD`. The api refuses to start with `ENVIRONMENT=production` while the JWT secret or admin password are the shipped defaults.
- Containers: api / worker / beat have SEPARATE images even though they share the `backend/` build context. Rebuild all relevant services after editing — building `api` alone leaves worker/beat stale.

## Git Hygiene

- Small, coherent commits and PRs. See `CONTRIBUTING.md`.
- Never commit `.env`, `.env.local`, generated secrets, personal identifiers (chat ids, domains, cloud project ids), or verbatim third-party data captures.
- New work → open an issue or add it to the roadmap in `docs/architecture.md`; don't silently expand scope.

## Child DOX Index

- [`backend/AGENTS.md`](backend/AGENTS.md) — Python service (FastAPI + Celery + Postgres). Owns API + workers + scheduler + tests + migrations.
- [`frontend/AGENTS.md`](frontend/AGENTS.md) — Next.js dashboard + shadcn/ui. Owns user-facing UI.
