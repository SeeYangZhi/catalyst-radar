# adapters/ — external data-source adapters

## Purpose

Every external data source has exactly one adapter here. Adapters are pure I/O + normalization — no DB writes, no notification logic, no LLM calls. Sync services in `../services/` orchestrate adapter calls + persistence.

## Ownership

All HTTP / blocking-library calls to external data sources for the backend. New source? New adapter file here.

## Local Contracts

### SourceAdapter base (`base.py`)

Every adapter that fronts a calendar-shaped source SHOULD subclass `SourceAdapter` and expose:

- `source_name: str` — short stable slug (used in source_runs + raw_items rows)
- `schema_name: str` — versioned schema tag (e.g. `eodhd.ipos.v1`); bump version when the upstream shape changes incompatibly
- `source_url: str` — upstream URL or human-readable endpoint for audit
- `async def fetch(target=...) -> FetchResult` — fetches raw payload; returns `FetchResult(source_name, schema_name, source_url, http_status, payload, items)` even on failure (use `http_status=0` for transport errors so the sync layer can record the failed run uniformly)
- `def normalize(raw_item) -> dict` — single-row normalization into the internal event shape
- `def source_event_id(raw_item) -> str` — STABLE across reruns. Format: `<source_slug>:<event_type>:<key>[:<sub>]`. Helper: `dedup.source_event_id(source, *parts)`
- `def dedup_key(raw_item) -> str` — `dedup.dedup_key(*parts)` (sha256 of joined parts). Same key on cross-source URL match collapses duplicates onto one Event

Some adapters (CNINFO, AAStocks) don't fit the strict `fetch(target)` shape; those expose specialized methods (`fetch_recent`, `download_pdf`, etc.) and are used directly by their service — fine, but document the surface in the file's module docstring.

### Persist raw first

Sync services persist the raw `FetchResult.payload` via `RawItemRepository.store()` BEFORE iterating `result.items`. If a normalization bug eats a row, the raw payload is still recoverable.

### Blocking libraries

`akshare`, `tushare`, `pandas` are sync. Wrap in `asyncio.to_thread(self._fetch_blocking)`. Never `await` them directly.

### Retry policy

Per-adapter when the upstream is known-flaky (CNINFO bursts; Eastmoney's `stock_ipo_review_em` chunked-encoding mid-pagination). Pattern: in-adapter `_MAX_RETRIES` constant + exponential backoff (5s, 20s, …) inside the blocking call. Don't retry network errors at the service layer — adapters own this.

### Identity / outbound safety

- HTTP scrapers of public sites (HKEXnews, CNINFO, MOPS) send `settings.scraper_user_agent` (`SCRAPER_USER_AGENT`) — an honest, descriptive UA. New scrapers do the same; don't spoof a browser UA.
- Downloads of URLs that come from a source payload or an LLM (prospectus PDFs, xlsx reports) go through `catalyst_radar/net_guard.py`, which refuses private / loopback / link-local addresses.
- Self-hosters are responsible for complying with each source's terms and rate limits; keep adapters polite (throttle, no parallel bursts).

### Geo / auth

- CNINFO: HTTP (not HTTPS), throttled ~1 req/s, no auth. Geo-egress from outside CN occasionally slow but works.
- tushare: per-tier rate-limit (1 call/hr for `share_float` at the 120-credit tier). Adapter must NEVER abort on rate-limit — log + return empty.
- EODHD / Telegram: API-key from `settings.<provider>_api_key`. `configured` property reflects key presence.
- SEC EDGAR: no key; `settings.sec_edgar_user_agent` must carry a contact per SEC fair-access guidance.

### One bad row never aborts a sync

Adapter `fetch` may return `items=[]` + non-200 status on failure. Service layer treats that as `status="failed" | "empty"` source_run and moves on. NEVER raise out of `fetch`.

## Work Guidance

### Adding a new source

1. Read this doc and `base.py`.
2. Pick a stable `source_name` slug. Confirm it's not in use.
3. Implement `fetch` + `normalize` + `source_event_id` + `dedup_key`. Decide whether `source_event_id` should merge with an existing adapter's events (use the same key pattern, e.g. akshare and CNINFO both use `<exchange>:ipo:<6-digit-code>` so they dedupe on the same Event row).
4. Add a corresponding sync service in `../services/` (do NOT couple persistence here).
5. Add a stub class in the test file that subclasses your adapter and overrides `fetch` or `_fetch_blocking` to return fixture data — no live HTTP in tests.

### Schema-name bumps

When the upstream JSON shape changes incompatibly, bump `schema_name` (e.g. `eodhd.ipos.v1` → `v2`). Old rows in `raw_items` keep the old tag; the analytics view filters by current schema.

## Verification

- New adapter must have at least one test stubbed by a fixture DataFrame / JSON envelope (no live network).
- Run: `cd backend && uv run pytest tests/test_<adapter>.py`.
- Fixtures are synthetic but real-shaped; never commit verbatim third-party captures (see `../../tests/AGENTS.md`).

## Current adapters

| File | Source | Used by |
|---|---|---|
| `eodhd.py` / `eodhd_calendar.py` / `eodhd_news.py` | EODHD (US earnings, US IPOs, news) | `earnings_sync`, `ipo_sync`, `catalyst_sync` |
| `aastocks_ipo.py` | AAStocks HK IPO calendar (Playwright-rendered) | `hk_ipo_sync` |
| `hkex_prospectus.py` | HKEXnews prospectus PDFs | `hk_ipo_enrich` |
| `hkex_newly_listed.py` | HKEXnews New Listing Report (per-year xlsx: code + listing date) | `hk_ipo_enrich.flip_listed_hk_ipos` |
| `akshare_ipo.py` | Eastmoney A-share IPO calendar (post-pricing) | `cn_ipo_sync` |
| `akshare_ipo_review.py` | CSRC IPO review-committee hearings (pre-listing) | `cn_ipo_review_sync` |
| `eastmoney_news.py` | Eastmoney per-ticker CN news (`stock_news_em`, dated) | `catalyst_sync` |
| `mops_announcements.py` | MOPS (mops.twse.com.tw) TW material-information announcements — JSON API, ROC calendar, link-less items | `catalyst_sync` |
| `cninfo_filings.py` | CNINFO 巨潮资讯网 prospectus filings | `cn_ipo_enrich` |
| `tushare_unlocks.py` | Tushare A-share share-float unlocks | `cn_unlocks_sync` |
| `sec_edgar.py` | SEC EDGAR registration filings | `edgar_enrich` |
| `websearch_news.py` | OpenAI web_search (gap-market news) | `catalyst_sync` |

## Child DOX Index

(none — adapters do not nest further)
