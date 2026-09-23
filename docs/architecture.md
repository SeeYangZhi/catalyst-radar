# Architecture

How Catalyst Radar is put together, and what is not built yet. For the product
requirements see [`product-specs/PRD-catalyst-radar.md`](product-specs/PRD-catalyst-radar.md);
for intentional departures from the PRD see [`decisions.md`](decisions.md).
Per-directory coding contracts live in the `AGENTS.md` files.

## Runtime components

| Component | Tech | Role |
|---|---|---|
| `api` | FastAPI (uvicorn) | Dashboard HTTP API under `/api/v1`, JWT auth, Telegram webhook. Runs `alembic upgrade head` on start. |
| `worker` | Celery | Runs every sync / enrich / dispatch job. |
| `beat` | Celery Beat | Schedules the jobs (`backend/catalyst_radar/celery_app.py`). Cadences are runtime-tunable. |
| `postgres` | PostgreSQL 17 | Events, notifications, tracked companies, raw payloads, source runs, runtime config. |
| `redis` | Redis 7 | Celery broker / result backend, login rate limiter. |
| `frontend` | Next.js + shadcn/ui | Admin dashboard. |
| `nginx` (prod) | nginx | Single origin: `/api/v1/*` to `api`, everything else to `frontend`. |

The `api`, `worker` and `beat` containers are built from the same `backend/`
context but are separate images; rebuild all three after backend changes.

## Pipeline: sync, enrich, dispatch

Each domain (US IPOs, HK IPOs, CN IPOs, CN IPO review stage, CN unlocks,
earnings, catalyst news) follows the same three steps:

1. **Sync.** An adapter fetches one source. The raw payload is stored in
   `raw_items` before normalization, each attempt is recorded in `source_runs`,
   and normalized rows are upserted into `events` keyed on a stable
   `source_event_id`. Matching against the tracked universe and IPO filters
   creates `event_relevance` rows and pending `notifications`, deduplicated by a
   deterministic `dedup_key`.
2. **Enrich.** Slower secondary fetches fill in event payloads: SEC EDGAR
   prospectuses (US), HKEXnews prospectus / allotment documents and listing
   confirmation (HK), CNINFO prospectus PDFs (CN), LLM business summaries, and
   earnings consensus. Enrichment is bounded per run and idempotent.
3. **Dispatch.** Pending notifications are rendered and sent to every
   eligible Telegram chat. Delivery is tracked per chat, failed sends are
   retried for the missing chats only, and IPO alerts are re-rendered at send
   time so late enrichment reaches the first alert. When a description lands
   after an alert went out, the delivered messages are edited in place.

Alert generation (sync) is always separate from delivery (dispatch), so a
Telegram outage never loses an alert.

## Catalyst news

Catalyst news is gathered per tracked company from several sweeps, in coverage
order: EODHD news (US / HK / KR), Eastmoney per-ticker news (CN listed),
MOPS material-information announcements (Taiwan), and OpenAI `web_search` for
markets without a ticker-keyed feed (CN pre-IPO, TW, CN). Each item goes
through:

1. freshness and undated-item gates;
2. deterministic keyword prefilters (drops awards, hackathons, marketing);
3. URL-exact dedup;
4. an OpenAI Responses API classifier with a strict Structured Outputs schema
   (importance, confidence, subtype, summary, suggested action);
5. semantic twin merge (canonical `story_key`, fuzzy title match with an LLM
   judge for the grey zone) so the same story from several outlets alerts once;
6. optional spillover assessment that flags related tracked companies
   (parents, subsidiaries, JV partners) affected by the event.

Every classifier decision is logged in `classifier_runs` and is reviewable in
the dashboard, with feedback labels (useful / not useful / false positive /
false negative).

## Hong Kong IPO identity

HK IPOs are discovered from the AAStocks IPO calendar (EODHD's HK calendar is
effectively post-listing). The zero-padded 5-digit stock code is the
cross-source key (`hkex:ipo:<code>`), so AAStocks, HKEXnews and EODHD rows for
the same listing collapse onto one event. Each source updates its own facet:
AAStocks sets dates and offer price, HKEXnews adds prospectus / allotment
links, and the HKEXnews New Listing Report flips the event to `listed`.

## Telegram bot

- Webhook mode when `TELEGRAM_WEBHOOK_URL` is set (requires public HTTPS),
  otherwise long polling from a beat task.
- Commands: `/start`, `/help`, `/watchlist`, `/today`, `/thisweek`,
  `/earnings`, `/ipo`, `/catalysts`, `/settings`.
- Inline buttons on alerts and lists to star or dismiss events. Stars,
  dismissals and listing-day reminders are stored per chat.
- Scheduled daily / weekly digests and a listing-day reminder for starred IPOs.
- Access model: see [`decisions.md`](decisions.md).

## Self-monitoring

A beat watchdog alerts the admin chats when ingestion stalls (no successful
source run within a window), a single source goes stale or keeps failing, or a
provider quota trips. `source_runs` older than the retention window are pruned
daily.

## Roadmap / known gaps

Not built yet; contributions welcome.

- **OpenDART mapping (KR).** `company_reference.corp_code` exists but nothing
  fills it, so Korean disclosure lookup and KR disclosure catalysts are absent.
  KR catalysts currently come only from EODHD news.
- **IR / newsroom feed polling.** `source_discovery` finds per-company IR, blog
  and RSS feeds into `company_sources`, but nothing polls them.
- **EDGAR filing catalysts (US).** The SEC EDGAR adapter serves IPO prospectus
  enrichment only; filing-based catalyst detection from EDGAR submissions is
  unbuilt.
- **HKEX AP & PHIP pipeline.** The pre-hearing (Application Proof / PHIP)
  funnel for very early HK IPO signals is not implemented.
- **HKEXnews announcements feed.** HK catalysts arrive via EODHD news; there is
  no dedicated HKEXnews announcements sweep.
- **KR IPO monitoring** is out of scope for now.
