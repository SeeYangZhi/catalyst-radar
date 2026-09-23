# Catalyst Radar

Catalyst Radar is a self-hosted market-event monitor that sends high-signal
Telegram alerts for the companies and IPO markets you care about. It pulls IPO
calendars, earnings dates and company news from US, Hong Kong, mainland China,
Taiwan and Korea sources, filters them against your watchlist and IPO filters,
uses an LLM to separate real catalysts (approvals, launches, index inclusions,
lockup expiries, major deals) from noise (awards, hackathons, marketing), and
delivers deduplicated alerts to Telegram. A web dashboard lets you manage the
watchlist, tune filters, and review why each alert was sent or skipped.

## Features

- **IPO monitoring**
  - US: EODHD IPO calendar, enriched with SEC EDGAR prospectus data.
  - Hong Kong: upcoming IPOs from the AAStocks calendar, prospectus and
    allotment links from HKEXnews, and automatic "listed" close-out from the
    HKEXnews New Listing Report.
  - Mainland China: A-share IPO calendar (Eastmoney via akshare), the CSRC
    review-committee stage (pre-listing hearings), CNINFO prospectus
    extraction, pre-IPO company tracking, and tushare share-unlock alerts.
  - Country, exchange, sector keyword and deal-size filters; reminder windows
    before subscription and listing dates.
- **Earnings.** EODHD earnings calendar for tracked companies, with optional
  LLM-assisted consensus enrichment.
- **Company catalysts.** Per-company news from EODHD (US / HK / KR), Eastmoney
  (CN), MOPS material-information announcements (Taiwan) and OpenAI
  `web_search` for markets without a feed. Deterministic prefilters, an OpenAI
  Structured Outputs classifier, cross-outlet semantic dedup, and optional
  "spillover" alerts for related companies (parents, subsidiaries, partners).
- **Telegram bot.** Immediate alerts with inline star / dismiss buttons,
  `/today`, `/thisweek`, `/ipo`, `/earnings`, `/catalysts`, `/watchlist`,
  `/settings`; scheduled daily / weekly digests; listing-day reminders for
  starred IPOs; per-chat state; webhook or polling mode.
- **Web dashboard.** Company search and watchlist, IPO filters, earnings view,
  catalyst review with sent / skipped reasoning and feedback labels,
  notification history, source-run health, entity relationships, and runtime
  settings (cadences, toggles, digest times) without a redeploy.
- **Self-monitoring.** Telegram alerts to the admin when ingestion stalls, a
  source keeps failing, or a provider quota trips.

## Architecture

```
            ┌──────────── Celery beat (schedules) ────────────┐
            ▼                                                  │
  Celery worker:  sync ──► enrich ──► dispatch ──► Telegram Bot API
   (adapters)      │          │           │
                   ▼          ▼           ▼
                 PostgreSQL (events, raw payloads, source runs, notifications)
                   ▲
  FastAPI api ◄────┴──── Next.js dashboard        Redis = Celery broker
```

- **Backend:** Python 3.13, FastAPI, SQLModel / async SQLAlchemy, Alembic,
  Celery + Redis, PostgreSQL, structlog.
- **Frontend:** Next.js, TypeScript, Tailwind CSS, shadcn/ui.
- **Pipeline:** each source has an adapter; a *sync* stores the raw payload,
  upserts normalized events idempotently and creates pending notifications; an
  *enrich* step backfills slower data (prospectuses, summaries); *dispatch*
  delivers and retries. Alert generation is separate from delivery.

More detail: [`docs/architecture.md`](docs/architecture.md) (including the
roadmap / known gaps) and [`docs/decisions.md`](docs/decisions.md).

## Quickstart (Docker Compose)

Requirements: Docker with the Compose plugin, a Telegram account, and ideally
EODHD and OpenAI API keys (see the table below for what works without them).

1. **Configure.**

   ```bash
   git clone https://github.com/SeeYangZhi/catalyst-radar.git
   cd catalyst-radar
   cp backend/.env.example backend/.env
   ```

   Edit `backend/.env` and set at least `EODHD_API_KEY`, `OPENAI_API_KEY`,
   `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ADMIN_CHAT_ID` (next steps).

2. **Create a Telegram bot.** Message [@BotFather](https://t.me/BotFather),
   send `/newbot`, and put the token in `TELEGRAM_BOT_TOKEN`.

3. **Set your admin chat id.** By default the bot only talks to chats listed in
   `TELEGRAM_ADMIN_CHAT_ID` (comma-separated). Get your numeric id from
   [@userinfobot](https://t.me/userinfobot), or just send `/start` to your
   bot: a chat that isn't an admin gets a reply with its chat id. Leaving it
   empty treats every chat as admin (the api refuses to start that way in
   production). To let other people subscribe, see `TELEGRAM_OPEN_SUBSCRIBE`
   below.

4. **Start the stack.**

   ```bash
   docker compose --env-file backend/.env -f docker-compose.dev.yml up --build
   ```

   `--env-file backend/.env` is required: compose reads `POSTGRES_PASSWORD`
   from it. The first build takes a while (it installs headless Chromium for
   the HK IPO scraper). Migrations run automatically when the api starts.

5. **Log in.** Open <http://localhost:3000> and sign in with
   `DEFAULT_ADMIN_EMAIL` / `DEFAULT_ADMIN_PASSWORD` from `backend/.env`
   (`admin@radar.local` / `admin123!` by default; change it). The API is on
   <http://localhost:8000> (`/api/v1/health`, OpenAPI docs at `/docs`).

6. **Use it.** Send `/start` to your bot, add companies under
   *Company Search*, set IPO countries and filters under *IPO Filters* and
   *Alert Settings*. Syncs run on a schedule (most every few hours); trigger one
   immediately with:

   ```bash
   docker compose --env-file backend/.env -f docker-compose.dev.yml exec worker \
     uv run celery -A catalyst_radar.celery_app:celery_app call catalyst_radar.sync_ipos
   ```

For production (hardened compose file, nginx, TLS, backups) see
[`deploy/README.md`](deploy/README.md), an example single-VM deployment.

## Data sources & API keys

Every key is optional: a source whose key is missing is recorded as *skipped*
(not failed) and the self-monitor ignores it.

| Source | Used for | Key / config | Without it |
|---|---|---|---|
| [EODHD](https://eodhd.com) | Company reference (US / HK / KR / TW), earnings calendar, US IPO calendar, news for US / HK / KR | `EODHD_API_KEY` (paid plans; the news and calendar endpoints need a plan that includes them) | No company search universe, earnings, US IPOs or US / HK / KR news. Companies can still be added manually. |
| [OpenAI](https://platform.openai.com) | Catalyst classification and dedup judge, IPO business summaries, CN prospectus extraction, earnings consensus, `web_search` news sweeps, entity / relationship preflight | `OPENAI_API_KEY` | No catalyst alerts or LLM enrichment; calendar-based IPO / earnings alerts still work. |
| [Telegram Bot API](https://core.telegram.org/bots/api) | Alert delivery and bot commands | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ADMIN_CHAT_ID` | No alerts (dashboard only). |
| [tushare](https://tushare.pro) | CN A-share share-unlock calendar | `TUSHARE_API_TOKEN` | No CN unlock alerts. |
| [SEC EDGAR](https://www.sec.gov/os/accessing-edgar-data) | US IPO prospectus enrichment | No key. Set `SEC_EDGAR_USER_AGENT` to a descriptive string with your contact email, per SEC fair-access rules. | — |
| AAStocks | Upcoming HK IPO calendar (rendered with headless Chromium) | Keyless public web | — |
| HKEXnews | HK prospectus / allotment documents, New Listing Report | Keyless public web | — |
| Eastmoney (via [akshare](https://github.com/akfamily/akshare)) | CN IPO calendar, CSRC review stage, per-ticker CN news | Keyless public web | — |
| CNINFO | CN prospectus filings | Keyless public web | — |
| MOPS (Taiwan) | TW material-information announcements | Keyless public JSON | — |

The HKEXnews, CNINFO and MOPS scrapers identify themselves with `SCRAPER_USER_AGENT` (default
`CatalystRadar/0.1 (+https://github.com/SeeYangZhi/catalyst-radar)`). Set it to
something that identifies your deployment and a contact URL.

## Configuration

- **`backend/.env`** holds secrets and deployment values; the full contract is
  in [`backend/.env.example`](backend/.env.example). Every setting in
  [`backend/catalyst_radar/config.py`](backend/catalyst_radar/config.py) can be
  overridden by an environment variable of the same name in upper case.
- **Runtime settings** (sync cadences, feature toggles, digest hour / day,
  alert timezone, IPO filters) are edited in the dashboard's *Alert Settings*
  page and stored in the database; no restart needed except for cadences
  marked "restart required".
- Notable settings:

  | Variable | Default | Meaning |
  |---|---|---|
  | `TELEGRAM_ADMIN_CHAT_ID` | empty | Comma-separated admin chat ids. Admins can change `/settings` toggles and receive ops alerts. |
  | `TELEGRAM_OPEN_SUBSCRIBE` | `false` | `false`: only admin chats can use the bot. `true`: anyone who finds the bot can `/start`, receive alerts, and keep their own stars / dismissals; they can see your watchlist and alert stream. |
  | `TELEGRAM_WEBHOOK_URL` / `TELEGRAM_WEBHOOK_SECRET` | empty | Set for webhook mode (needs public HTTPS); otherwise the bot polls. |
  | `ALERT_TIMEZONE` | `UTC` | Local timezone for digests, reminders and "today". Also editable at runtime. |
  | `SCRAPER_USER_AGENT` | see above | User-Agent for the public-web scrapers. |
  | `SEC_EDGAR_USER_AGENT` | placeholder | Must include your contact for SEC EDGAR. |
  | `JWT_SECRET_KEY`, `DEFAULT_ADMIN_PASSWORD`, Postgres password | dev defaults | With `ENVIRONMENT=production` the api refuses to start while any of these is still the default, or while a bot token is set without `TELEGRAM_ADMIN_CHAT_ID`. |

## Development

```bash
# Backend (Python 3.13 via uv)
cd backend
uv sync
uv run pytest -q
uv run ruff check .

# Frontend (Bun)
cd frontend
bun install
bun dev             # http://localhost:3000, expects the API on :8000
bun run typecheck && bun run lint && bun run test
```

Run Postgres and Redis from the dev compose file
(`docker compose --env-file backend/.env -f docker-compose.dev.yml up postgres redis`)
and point `backend/.env` at `localhost`. More commands:
[`docs/references/commands.md`](docs/references/commands.md). Contribution
guidelines: [`CONTRIBUTING.md`](CONTRIBUTING.md). The repo uses `AGENTS.md`
files as per-directory contracts (useful to human and AI contributors alike);
UI conventions are in [`DESIGN.md`](DESIGN.md).

## Disclaimers

- **Not investment advice.** Catalyst Radar is a monitoring tool. Its alerts,
  classifications and summaries are informational only and are not financial,
  investment, legal or tax advice. Do your own research.
- **Data accuracy is not guaranteed.** Data comes from third-party sources and
  LLM output, both of which can be late, incomplete or wrong. Always verify
  against the primary source before acting.
- **Scraped public sources.** Several adapters read public websites (HKEXnews,
  AAStocks, CNINFO, Eastmoney, MOPS, SEC EDGAR). If you run this software, you
  are responsible for complying with each source's terms of use, robots
  directives and rate limits, and for holding valid subscriptions for paid
  APIs (EODHD, OpenAI, tushare). The maintainers are not affiliated with any of
  these providers. The scraped sources each have an on / off toggle in
  *Alert Settings*; disable any you are not permitted to use.

## License

Licensed under the [Apache License, Version 2.0](LICENSE). See [`NOTICE`](NOTICE).
Security issues: see [`SECURITY.md`](SECURITY.md).
