# services/ — business logic

## Purpose

Service modules orchestrate adapter fetches, persistence, dedup, alert generation, and dispatch. Adapters do I/O + normalize; services do everything else end-to-end for a domain.

## Ownership

All non-trivial business logic for the backend. Thin Celery wrappers in `../tasks.py` import these. API routes in `../api/v1/` should call services or repositories, never inline business logic.

## Local Contracts

### Sync / Enrich / Dispatch split

Three roles per domain, each its own function (often its own file):

- **Sync** (e.g. `ipo_sync`, `cn_ipo_review_sync`, `earnings_sync`, `catalyst_sync`) — pulls from one adapter, upserts events, decides matched/relevance, creates pending notifications. Returns a per-domain `*SyncSummary` dataclass.
- **Enrich** (e.g. `edgar_enrich`, `hk_ipo_enrich`, `cn_ipo_enrich`, `earnings_enrich`) — backfills payloads on existing events with slower secondary fetches (PDFs, LLM summaries). Bounded by `*_max_items_per_run`. Idempotent via a `payload.profile.checked` flag.
- **Dispatch** (`dispatch.deliver_pending_notifications`) — sends pending/failed notifications to every active Telegram chat (`TelegramChatRepository.active()`), rendering each recipient's own ⭐/☆ keyboard state. Per-chat delivery is tracked in `payload["delivered_chats"]`; partial fan-out stays "failed" and retries only missing chats. Per-notification poison-pill guard: an unexpected exception (raising client, formatter crash) marks that row "failed" and the loop continues — the queue orders by `created_at`, so an aborting row would otherwise block all later notifications. Every successful send records `payload["message_ids"][chat_id]` and `payload["chat_texts"][chat_id]` (the exact text that chat got); the singular `chat_id` / `telegram_message_id` columns keep only the first delivery (all that rows from before per-chat tracking have). `alert_backfill` edits every chat's copy of an IPO alert when a late description lands, idempotent per chat via `chat_texts`.

Celery tasks chain these inline when ordering matters (sync → enrich → dispatch in one task, so a freshly enriched description flows into the first alert). See `tasks.py::task_sync_ipos`.

### HK IPO lifecycle close

`hk_ipo_enrich.flip_listed_hk_ipos` flips open HK IPO events to `status="listed"` when their code appears in the HKEXnews New Listing Report (`adapters/hkex_newly_listed.py`). Idempotent (listed rows excluded from the candidate query); user-`ignored` rows are never flipped. `hk_ipo_sync` skips listed events before relevance/notification creation, so a listed IPO produces no further reminders. The flip rides `task_sync_hk_ipos` (BEFORE the sync, so the first run after a listing can't create a day-of reminder) and `task_enrich_hk_ipos` — no beat entry of its own. The flip-before-sync ordering is a contract: `sync_hk_ipos` only skips events already at `status="listed"`, so any direct caller must run `flip_listed_hk_ipos` first (documented on the `sync_hk_ipos` docstring). `payload.listing_confirmed` survives sync refreshes via `EventRepository.upsert`'s merge-by-default policy (keys absent from the incoming source payload are preserved). Gated by `hkex_listed_flip_enabled` (runtime-editable, default on).

### Telegram subscribers & per-chat flags

Access model (rationale in `docs/decisions.md`):

- `TELEGRAM_OPEN_SUBSCRIBE=false` (default): only chats in `TELEGRAM_ADMIN_CHAT_ID` (comma-separated, `settings.telegram_admin_chat_ids`) can `/start` and use the bot.
- `TELEGRAM_OPEN_SUBSCRIBE=true`: any chat can `/start` and receive alerts, digests, and browse commands.
- Empty `TELEGRAM_ADMIN_CHAT_ID` = every chat is admin (first-setup convenience only; deployments must set it).
- Admin chats alone gate global actions in both modes: `/settings` toggle taps (`telegram_bot._is_admin_chat`) and ops/monitor alerts (`TelegramChatRepository.admins()`).
- `event_flags` (star / dismiss / `day_of_notified_at`) is keyed on `(event_id, chat_id)` — every `EventFlagsRepository` call takes the acting chat's id, and list views, digests, alert keyboards and listing-day reminders all read that chat's own flags.

### Idempotency

- Sync upserts via `EventRepository.upsert(event)` — keyed on `source_event_id`. Re-runs are no-ops.
- Notifications dedup on `dedup_key` = `make_dedup_key(sid, "<window-or-stage>")`. Same window+event → no second notification.

### Alert formatters (`alerts.py`)

One formatter per event type: `format_earnings`, `format_ipo`, `format_catalyst`, `format_unlock`. They return Telegram HTML strings clipped to `_MAX_LEN`. Multi-shape events use a `payload["stage"]` (or similar) discriminator inside the formatter — e.g. `format_ipo` branches to `_format_cn_ipo_review` when `payload["stage"] == "review"`.

**Re-render on dispatch**: `dispatch.deliver_pending_notifications` re-renders `format_ipo(event)` at send time when `event_type == "ipo"` and persists the fresh text back to `notification.payload["text"]`. This lets enrichment that runs between notification creation and dispatch flow into the first alert. Other event types are not re-rendered (no enrichment path).

### Catalyst pipeline module map

The catalyst news pipeline spans four modules; `catalyst_sync` is the public entry point and the seam module tests rely on:

- `catalyst_sync.py` — thin orchestrator. `sync_catalysts` builds the gate/dedup configs + enricher, runs the sweeps in coverage order (EODHD → Eastmoney → MOPS → web_search), purges stale ignored rows, returns `CatalystSyncSummary`. **Seams:** conftest monkeypatches `catalyst_sync.utcnow` (the pipeline clock freeze — moved code resolves the clock through this attribute at call time via `catalyst_classify._utcnow`), and every moved name is re-exported here. Keep the re-exports in sync when touching the child modules.
- `catalyst_sources.py` — per-source sweeps (`_sync_eodhd_catalysts`, `_sync_eastmoney_catalysts`, `_sync_mops_catalysts`, `_sync_websearch_catalysts`) + `_url_probe_stats`. Each sweep: source_run wrap, raw_items store, fail-soft per company, items into `_process_news_item`. `_sync_eodhd_catalysts` returns `(budget, aborted)` — `aborted=True` on `OpenAIConfigError` makes the orchestrator return immediately (later sweeps would hit the same wall).
- `catalyst_classify.py` — per-item driver `_process_news_item` (freshness/undated gates → deterministic prefilter → URL-exact dedup → classifier → semantic twin merge → repeat-alert window → spillover → event + notification) plus `_Gate` / `_Dedup` / `CatalystSyncSummary` and the dedup helpers.
- `catalyst_enrich.py` — catalyst-news earnings-enrichment wiring only (`build_earnings_enricher`, `maybe_enrich_earnings`, `_ENRICH_SUBTYPES`). EDGAR / HK-IPO / CN-IPO enrichment live in their own `*_enrich` services dispatched from `tasks.py`.

### Dedup layers (catalyst news)

`catalyst_classify._find_semantic_twin` collapses same-story news from different outlets via three layered checks:

1. **`story_key` exact match** — fast, deterministic, language-blind. The classifier emits a canonical event identifier (e.g. `a25310_star_ipo_approval_2026-05-28`) under tight prompt rules; same-`(symbol, story_key)` → merge, no LLM call. Bypassed when one side lacks a v2 `story_key`.
2. **Fuzz title/summary + LLM judge** — `rapidfuzz.token_set_ratio` on title + classifier summary. `>= title_threshold` (88) auto-merges; `>= grey_threshold` (55) routes the BEST-scoring candidate to a one-shot `OpenAIClassifier.same_event` judge.
3. **Subtype fallback** — when nothing clears grey, judge against the most-recent same-`(symbol, subtype)` candidate in window. Catches paraphrase clusters fuzz misses entirely.

Window: `catalyst_dedup_window_hours` (72h). All thresholds in `config.py`; only `catalyst_dedup_llm_judge_enabled` is runtime-editable.

### Spillover / affected companies

`affected_companies.assess_affected_companies` runs after twin-merge but before notification creation. For propagation-enabled tracked companies, it expands a primary event into "affects your watchlist" rows and bakes them into the event payload. Order matters: assess BEFORE inserting `EventRelevance` (matched=True is the canonical UI signal; an orphaned matched-row on a dropped event surfaces silently-dropped events).

### Lifecycle (`lifecycle.py`)

`is_pre_ipo_cn_symbol(symbol, exchange)` is the single source of truth for whether a tracked company is pre-IPO (CSRC reservation code, A-prefix). Pre-IPO rows skip ticker-keyed sources (EODHD news 404s on A-prefix codes) but stay in web_search-driven paths (preflight, source discovery, web_search news). Reuse this helper for any new fork — do not re-derive.

### Runtime config

Everything user-tunable goes through `runtime_config.effective(session)`. Never reach into `Settings` directly inside a service when there's an `EDITABLE` key for it — that bypasses the UI override. Cadences read at boot by Celery Beat are listed in `RESTART_REQUIRED`.

### Best-effort outer try

Same as adapters: one bad event must not abort a sync. Wrap per-item processing in `try/except Exception` with a structured-log warning + `continue`. Top-level commit at end of loop.

## Work Guidance

- New domain sync: model after `cn_ipo_review_sync.py` (clean shape — adapter call, source_run wrap, raw_items store, event upsert loop with skip counters, notification dedup, commit, summary).
- New formatter: add to `alerts.py`, route from `format_<type>` by payload discriminator. If it needs dispatch re-render, extend the `event.event_type` check in `dispatch.py:114`.
- New dedup signal for catalyst news: extend `catalyst_classify._find_semantic_twin` — keep "1 LLM judge call per item max" as the cost ceiling.
- New catalyst news source: add a `_sync_<source>_catalysts` sweep to `catalyst_sources.py` (mirror the MOPS one), call it from `sync_catalysts` in coverage order, and thread a summary counter through `CatalystSyncSummary`.
- Adding a runtime-tunable knob: add to `Settings`, add to `runtime_config.EDITABLE` (and `RESTART_REQUIRED` if read at Beat startup), read via `await effective(session)`.

## Verification

- `cd backend && uv run pytest tests/test_<service>.py`.
- For dispatch / dedup changes: also run `tests/test_catalyst_sync.py` (covers the three-layer dedup).
- Smoke test on a running stack: trigger the relevant Celery task via `celery -A catalyst_radar.celery_app:celery_app call <name>` and inspect `source_runs` + `events` / `notifications` rows.

## Child DOX Index

(none)
