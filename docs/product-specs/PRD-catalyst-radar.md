# PRD: Catalyst Radar

> Original product spec, kept as the requirements reference. The shipped system extends it (CN IPOs incl. the CSRC review stage, Taiwan news via MOPS). See [`../architecture.md`](../architecture.md) for what is built and [`../decisions.md`](../decisions.md) for intentional deviations.

## 1. Summary

Catalyst Radar is a small authenticated financial monitoring app for one primary user. It tracks IPOs in the US and Hong Kong, plus earnings and company-specific catalysts across US, Hong Kong, and Korea, then sends high-signal Telegram alerts based on user-defined companies, countries, and sectors.

The app is standalone and self-hosted, with modular source adapters and a stable event schema so it can be embedded in a larger market-data system later.

## 2. Roles

| Role | Responsibility |
|------|----------------|
| Operator / primary user | Self-hosts the app; defines tracked companies, IPO filters, and the alert quality bar |
| Contributors | Extend sources, classifiers, and UI following the repo's `AGENTS.md` contracts |

## 3. Background

Broad trend-capture and market-news platforms are heavier than needed for a focused, Telegram-first catalyst monitor.

Catalyst Radar solves a narrower job:

- Track companies the user cares about.
- Track IPOs by country and sector.
- Track broad earnings calendars, but notify only based on user-side filters.
- Detect company-critical news and disclosure events.
- Send useful Telegram alerts without generic market noise.

The app follows a "single admin with authentication" pattern: a real users table, a seeded admin, JWT login, and protected admin APIs. It does not build multi-tenant behavior.

## 4. Objective

### Objective

Give the user a reliable self-hosted alerting system for IPO, earnings, and company catalyst events, starting with US/Hong Kong IPO coverage and US/Hong Kong/Korea company coverage.

### Why It Matters

The user does not need another market news feed. They need a system that tracks a defined universe and says: "This event matters to your watchlist, here is why, and here is the source."

### Key Results

| Key Result | Target |
|------------|--------|
| KR1: Setup speed | User can add first tracked companies and Telegram chat within 15 minutes of deployment |
| KR2: Earnings coverage | Ingest earnings calendars daily and notify for tracked companies in US/HK/Korea where source data is available |
| KR3: IPO coverage | Ingest IPO candidates for US/HK and filter by user-selected country and sector |
| KR4: Catalyst precision | At least 80% of delivered catalyst alerts are judged useful by the user during the first review period |
| KR5: Noise control | Minor company items such as hackathons, generic awards, and routine marketing posts are skipped by default |
| KR6: Delivery reliability | No duplicate Telegram alert for the same event after repeated source syncs |

## 5. Market Segment

### Primary Segment

One active discretionary trader or investor who follows specific public companies, IPO markets, and themes.

### Jobs To Be Done

- "When a tracked company has a critical event, tell me quickly."
- "When an IPO matches my country and sector interests, alert me with enough context to decide if I should research it."
- "When a company has earnings coming up, remind me with date, expectations, and relevant context."
- "When I remove a company, stop tracking it and remove its old local data if requested."

### Constraints

- Single-user product behavior at MVP.
- Authentication still required.
- Telegram is the primary delivery channel.
- Sources will vary in quality by market.
- HK and Korea must be day-one markets for company reference, earnings where available, and company catalyst monitoring.
- Korean IPO monitoring is not required for day one.
- No Financial Modeling Prep dependency.
- No automated trading.
- No broad social/trend graph in MVP.

## 6. Value Propositions

### Value Proposition 1: Personal Relevance

The system only alerts for companies, sectors, countries, and event types the user has configured.

### Value Proposition 2: Source-Backed Alerts

Every alert should include a source link or source identity. The user should be able to inspect the original item.

### Value Proposition 3: Company-Critical Catalyst Filtering

The app uses deterministic filters and LLM classification to exclude low-value items like hackathons, generic awards, and routine posts.

### Value Proposition 4: Fast Enough, Not Terminal-Complex

The app should be simple to run and maintain. It should not require a large platform deployment, gRPC, trend clustering, or multi-agent orchestration.

## 7. Solution

### 7.1 Product Modules

This PRD suite defines nine product modules:

1. App Foundation and Authentication
2. Company Tracking
3. IPO Monitoring
4. Earnings Monitoring
5. Catalyst Monitoring
6. Telegram Alerts
7. Admin Web Interface
8. Data Sources and Adapters
9. Portability

---

# Module PRD 1: App Foundation and Authentication

## Summary

Create the standalone Catalyst Radar app with single-admin authentication: seeded admin user, JWT login, protected APIs, and simple app configuration.

## Requirements

- Backend uses FastAPI, SQLModel, async SQLAlchemy, PostgreSQL, Alembic, Celery, Redis, Pydantic Settings, and structlog.
- Frontend uses Next.js, TypeScript, Tailwind, shadcn/ui, and Bun.
- Use `uv` for Python package management.
- Use `.env` for secrets.
- Seed a default admin account:
  - Email: `admin@radar.local`
  - Password: `admin123!`
  - Role: admin
- Registration is disabled by default.
- All admin APIs require JWT bearer auth.
- App can run locally with Docker Compose.

## Key Features

- `POST /api/v1/auth/token`
- Optional disabled `POST /api/v1/auth/register`
- `GET /api/v1/me`
- `GET/PUT /api/v1/settings`
- Startup seed for default admin if no users exist
- Health endpoint

## Acceptance Criteria

- User can log in from the web UI.
- Protected APIs reject unauthenticated requests.
- Default admin is created on first startup.
- Registration can be controlled by config but defaults off.
- No multi-user sharing, organization, team, or permission system is included.

---

# Module PRD 2: Company Tracking

## Summary

Allow the authenticated admin user to search a preloaded company universe, then add, remove, and manage tracked companies. These companies drive earnings alerts and catalyst monitoring.

## Requirements

- Maintain a searchable reference list of companies before the user starts adding tracked companies.
- Seed/sync the reference list for US, Hong Kong, Korea Exchange, and KOSDAQ on day one.
- Let the user search by ticker, company name, exchange, country, and alias.
- Let the user add a tracked company from a search result.
- Allow manual company creation only as a fallback when a company is missing from the reference list.
- Support US, HK, Korea Exchange, and KOSDAQ symbols on day one.
- Store aliases for better news and filing matching.
- Allow optional sector/theme tags.
- Support active/inactive status.
- Allow deletion of company-scoped past data when removing a company.

## Key Fields

`company_reference`

- `id`
- `symbol`
- `exchange`
- `country`
- `company_name`
- `sector`
- `industry`
- `currency`
- `isin`
- `aliases`
- `source`
- `source_payload`
- `is_active`
- `last_seen_at`
- `created_at`
- `updated_at`

`tracked_companies`

- `id`
- `company_reference_id`
- `symbol`
- `exchange`
- `country`
- `company_name`
- `sector`
- `themes`
- `aliases`
- `source`
- `source_payload`
- `is_active`
- `created_at`
- `updated_at`

## User Stories

- As the user, I can search for Tencent, select the correct HK listing, and add it to tracking.
- As the user, I can add SK Hynix and receive earnings/catalyst alerts.
- As the user, I can distinguish Korea Exchange and KOSDAQ results in search.
- As the user, I can manually add a missing company if the reference list does not include it.
- As the user, I can remove a company and choose whether to delete old local data.
- As the user, I can list all tracked companies from the web UI and Telegram.

## Acceptance Criteria

- Company search works before any company has been tracked.
- Search results include symbol, company name, exchange, country, sector if known, and source.
- Adding a company from search copies normalized metadata into `tracked_companies`.
- Manual company entry is available but clearly marked as fallback.
- Company list can be managed in the web UI.
- Telegram `/watchlist` returns active tracked companies.
- Removing a company stops future fetches and notifications.
- Optional data deletion removes company-scoped raw items, events, and notifications.

---

# Module PRD 3: IPO Monitoring

## Summary

Monitor IPOs in the US and Hong Kong. Notify when an IPO matches user-defined country and sector filters.

## Requirements

- User can configure IPO countries.
- User can configure IPO sectors/themes.
- Ingest broad IPO data, then filter by settings.
- Store all fetched IPO events with stable dedup keys.
- Notify only for matching IPOs.
- Include source links when available.

## Day-One Sources

- EODHD IPO calendar for structured IPO calendar ingestion.
- HKEX New Listing Information for HK IPO announcements, prospectuses, and allotment results.
- HKEXnews title/predefined document search for HK allotment result documents.

Source references:

- EODHD calendar endpoints: https://eodhd.com/financial-apis/calendar-upcoming-earnings-ipos-and-splits
- EODHD supported exchanges, including `HK`: https://eodhd.com/financial-apis/list-supported-exchanges/
- HKEX New Listings: https://www2.hkexnews.hk/New-Listings/New-Listing-Information/Main-Board?sc_lang=en
- HKEX IPO allotment FAQ: https://www.hkex.com.hk/Global/Exchange/FAQ/Products/Securities/Equity-Securites?sc_lang=en

## Alert Content

- Company name
- Ticker or proposed ticker, if known
- Exchange
- Country
- IPO/listing date
- Sector/theme tags
- Source link
- Valuation or offer size if available
- HK allotment/subscription/grey-market related fields if available
- Why it matters
- Priority

## Acceptance Criteria

- User can filter IPO alerts by country and sector.
- US and HK are first-class IPO country options.
- Korea IPO monitoring is excluded from the MVP and should not block day-one release.
- Duplicate IPO alerts are suppressed.
- IPOs without complete data are still shown if they match filters, with missing fields marked as unknown.
- Alerts are not sent for countries or sectors the user did not enable.

---

# Module PRD 4: Earnings Monitoring

## Summary

Ingest broad earnings calendar data and send Telegram alerts for tracked companies. Do not pre-filter ingestion globally.

## Requirements

- Fetch earnings calendars by date window.
- Store normalized earnings events.
- Notify when an event matches an active tracked company.
- User can configure alert windows, such as 7 days before, 1 day before, and same day.
- User can disable earnings alerts globally.
- No Financial Modeling Prep backup.

## Day-One Sources

- EODHD earnings calendar.
- EODHD exchange symbols for US, HK, KO, KQ mapping where available.

## Alert Content

- Company name
- Ticker
- Exchange
- Earnings date/time if available
- Fiscal period if available
- Consensus expectations if available
- Source
- Why it matters: direct tracked company earnings

## Acceptance Criteria

- Running ingestion twice does not create duplicate events.
- Earnings alerts are created only for tracked companies.
- User can see upcoming tracked-company earnings in `/earnings`, `/today`, and `/thisweek`.
- Missing consensus data does not block alerts.

---

# Module PRD 5: Catalyst Monitoring

## Summary

Monitor company-specific news, disclosures, and official announcements for tracked companies. Use LLM classification to decide whether an item is company-critical.

## Requirements

- Fetch raw items only for active tracked companies.
- Deduplicate by source ID, URL, title/date, and content hash.
- Run LLM classification only after cheap deterministic checks.
- Exclude minor or low-value items.
- Persist both raw item and classification output.
- Create catalyst events for important items.

## Day-One Sources

- EODHD financial news for ticker-linked company news where available.
- HKEXnews announcements for HK listed companies.
- OpenDART disclosure search for Korean companies.
- Optional company IR/newsroom pages for selected tracked companies.

Source references:

- EODHD financial news API: https://eodhd.com/financial-apis/stock-market-financial-news-api
- OpenDART disclosure search: https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DE001&apiId=AE00001

V2 source references:

- SEC EDGAR APIs: https://www.sec.gov/search-filings/edgar-application-programming-interfaces

## Catalyst Types

- Product launch
- Major business development
- Major partnership
- Major customer announcement
- Investor day
- Analyst day
- Regulatory decision
- Share unlock / lockup expiry
- IPO / uplisting rumor
- Guidance change
- Management change
- Major financing or issuance
- Other company-critical event

## Explicitly Ignore

- Hackathons
- Minor awards
- Routine marketing posts
- Generic ESG/community content
- Minor conference attendance
- Reposted old news
- Articles where the company is only casually mentioned

## LLM Output Schema

```json
{
  "is_company_critical": true,
  "event_subtype": "product_launch",
  "importance": "medium",
  "confidence": 0.82,
  "expected_impact": "Potential revenue and narrative impact for cloud segment.",
  "summary": "Tencent launched...",
  "why_it_matters": "This matters because...",
  "suggested_action": "research",
  "ignore_reason": null
}
```

## Acceptance Criteria

- Catalyst alerts are only generated for active tracked companies.
- LLM output is stored for review.
- Low-value items are skipped with an ignore reason.
- User can review catalyst history in the web UI.
- Duplicate articles about the same event do not trigger duplicate Telegram alerts.

---

# Module PRD 6: Telegram Alerts

## Summary

Send earnings, IPO, and catalyst alerts through Telegram. Telegram is the primary delivery channel.

## Requirements

- User can set Telegram chat ID through `/start` or web settings.
- Only the authenticated admin's configured chat should receive alerts (see `docs/decisions.md` for the optional open-subscribe mode).
- Support commands:
  - `/start`
  - `/watchlist`
  - `/add TICKER`
  - `/remove TICKER`
  - `/today`
  - `/thisweek`
  - `/ipo`
  - `/earnings`
  - `/catalysts`
  - `/settings`
- Support immediate alerts and digest mode.
- Track sent notifications to prevent duplicates.

## Message Requirements

IPO message:

- Company / ticker / exchange
- Country / sector
- IPO date
- Source link
- Why it matters
- Priority

Earnings message:

- Company / ticker / exchange
- Earnings date/time
- Consensus if available
- Source

Catalyst message:

- Company / ticker
- Event type
- Summary
- Expected impact
- Confidence
- Source link
- Suggested action

## Acceptance Criteria

- `/start` can register the configured Telegram chat.
- User can request today's and this week's relevant events.
- Every sent alert is recorded in `notifications`.
- Failed sends are retried with backoff.
- Telegram messages stay within Telegram limits and do not break HTML formatting.

---

# Module PRD 7: Admin Web Interface

## Summary

Provide a simple authenticated UI for managing companies, IPO filters, settings, and notification history.

## Pages

- Login
- Dashboard
- Tracked Companies
- Company Search / Add Company
- Add/Edit Company
- IPO Filters
- Alert Settings
- Events
- Notifications
- Source Runs / Health

## Dashboard Content

- Active tracked companies count
- Upcoming earnings count
- Matching IPO count
- Recent catalyst alerts
- Last source run status
- Telegram status

## Acceptance Criteria

- User can manage all settings without editing `.env`.
- User can search the preloaded company universe before adding a tracked company.
- Search supports ticker/name and filters for country/exchange.
- User can inspect why a notification was sent or skipped.
- User can see source failures.
- UI is compact and operational, not a marketing site.

---

# Module PRD 8: Data Sources and Adapters

## Summary

Use source adapters so data ingestion stays modular and adapters can be reused elsewhere.

## Adapter Interface

Each adapter should expose:

- `source_name`
- `fetch(window_or_company)`
- `normalize(raw_item)`
- `dedup_key(raw_or_normalized_item)`
- `source_event_id(raw_item)`

## Required Adapters

- `EodhdCompanyReferenceAdapter`
- `EodhdEarningsAdapter`
- `EodhdIpoAdapter`
- `EodhdNewsAdapter`
- `HkexNewListingsAdapter`
- `HkexAnnouncementsAdapter`
- `OpenDartDisclosureAdapter`
- `CompanyIrPageAdapter`

Version 2 adapters:

- `SecEdgarAdapter`

Optional future adapters:

- `MassiveNewsAdapter`
- Additional broker or vendor adapters for HK grey-market/subscription data

## Source Response Contracts

All adapters must persist the raw response before normalization. Store the raw body or parsed JSON in `raw_items.raw_payload`, plus `source_name`, `source_url`, `http_status`, `fetched_at`, and a `schema_name` such as `eodhd.exchange_symbol_list.v1`. Parsers must tolerate missing optional fields, unknown extra fields, HTML error bodies, and provider-specific null values.

### EODHD Exchange List

Endpoint: `GET /api/exchanges-list/`

Response shape: an array of exchange records.

```json
[
  {
    "Name": "USAStocks",
    "Code": "US",
    "OperatingMIC": "XNAS,XNYS",
    "Country": "USA",
    "Currency": "USD",
    "CountryISO2": "US",
    "CountryISO3": "USA"
  },
  {
    "Name": "LondonExchange",
    "Code": "LSE",
    "OperatingMIC": "XLON",
    "Country": "UK",
    "Currency": "GBP",
    "CountryISO2": "GB",
    "CountryISO3": "GBR"
  }
]
```

Normalization:

- Upsert into `exchanges` or app settings cache by `Code`.
- Keep `OperatingMIC` as a comma-delimited source field and derive a normalized MIC array.
- Use `CountryISO2` and `CountryISO3` for country filters instead of free-text `Country`.

### EODHD Exchange Symbol List

Endpoint: `GET /api/exchange-symbol-list/{exchangeCode}`

Response shape: an array of instruments for one exchange.

```json
[
  {
    "Code": "CDR",
    "Name": "CD PROJEKT SA",
    "Country": "Poland",
    "Exchange": "WAR",
    "Currency": "PLN",
    "Type": "Common Stock",
    "Isin": "PLOPTTC00011"
  },
  {
    "Code": "PKN",
    "Name": "PKN Orlen SA",
    "Country": "Poland",
    "Exchange": "WAR",
    "Currency": "PLN",
    "Type": "Common Stock",
    "Isin": "PLPKN0000018"
  }
]
```

Normalization:

- Upsert into `company_reference` by `(Exchange, Code)`.
- Store `Code` as provider symbol and derive app symbol as `Code.Exchange` when needed.
- Store `Isin` as nullable.
- Default day-one exchange sync list: `US`, `HK`, `KO`, and `KQ`.
- Include only equity-like instruments in default company search, but keep other `Type` values in raw payloads.

### EODHD Symbol Search

Endpoint: `GET /api/search/{query_string}`

Response shape: an array of search results.

```json
[
  {
    "Code": "AAPL",
    "Exchange": "US",
    "Name": "Apple Inc",
    "Type": "Common Stock",
    "Country": "USA",
    "Currency": "USD",
    "ISIN": "US0378331005",
    "previousClose": 189.98,
    "previousCloseDate": "2026-05-15",
    "isPrimary": true
  }
]
```

Use this as a fallback resolver when the local `company_reference` search misses, not as the primary UI search path.

### EODHD Earnings Calendar

Endpoint: `GET /api/calendar/earnings`

Response shape: an object with metadata and an `earnings` array. Some live responses may also include `symbols`.

```json
{
  "type": "Earnings",
  "description": "Historical and upcoming Earnings",
  "from": "2026-05-01",
  "to": "2026-05-31",
  "symbols": "AAPL.US",
  "earnings": [
    {
      "code": "AAPL.US",
      "report_date": "2026-05-07",
      "date": "2026-03-31",
      "before_after_market": "AfterMarket",
      "currency": "USD",
      "actual": 1.88,
      "estimate": 1.94,
      "difference": -0.06,
      "percent": -3.0928
    }
  ]
}
```

Normalization:

- Event type: `earnings`.
- Stable source event ID: `eodhd:earnings:{code}:{report_date}:{date}`.
- Treat `report_date` as the catalyst date and `date` as fiscal period end.
- Store `before_after_market`, `estimate`, `actual`, `difference`, and `percent` in event metadata.

### EODHD Earnings Trends

Endpoint: `GET /api/calendar/trends`

Response shape: an object with `trends`, where the provider schema represents results as nested arrays.

```json
{
  "type": "Trends",
  "description": "Historical and upcoming earning trends",
  "symbols": "AAPL.US",
  "trends": [
    [
      {
        "code": "AAPL.US",
        "date": "2026-09-30",
        "period": "+1q",
        "growth": "0.1200",
        "earningsEstimateAvg": "7.4700",
        "earningsEstimateLow": "6.8800",
        "earningsEstimateHigh": "7.9300",
        "earningsEstimateNumberOfAnalysts": "42",
        "revenueEstimateAvg": "420689000000.00",
        "revenueEstimateNumberOfAnalysts": "40",
        "epsTrendCurrent": "7.4700",
        "epsTrend30daysAgo": "7.4800",
        "epsRevisionsUpLast30days": "4",
        "epsRevisionsDownLast30days": "3"
      }
    ]
  ]
}
```

Normalization:

- Flatten nested trend arrays.
- Store numeric-looking strings as decimals where possible, but preserve the raw string values.
- Join to earnings alerts by `code` and nearest fiscal period/date when available.

### EODHD IPO Calendar

Endpoint: `GET /api/calendar/ipos`

Response shape: an object with metadata and an `ipos` array.

```json
{
  "type": "IPOs",
  "description": "Historical and upcoming IPOs",
  "from": "2026-05-01",
  "to": "2026-05-31",
  "ipos": [
    {
      "code": "N/A",
      "name": "Example Robotics Ltd",
      "exchange": "NASDAQ",
      "currency": "USD",
      "start_date": "2026-05-21",
      "filing_date": "2026-04-10",
      "amended_date": "2026-05-01",
      "price_from": 18.0,
      "price_to": 21.0,
      "offer_price": 20.0,
      "shares": 25000000,
      "deal_type": "Expected"
    }
  ]
}
```

Normalization:

- Event type: `ipo`.
- Stable source event ID: `eodhd:ipo:{exchange}:{code_or_name}:{start_date}:{deal_type}`.
- Day-one filtering should only enable US and HK IPOs.
- Korea IPOs are intentionally not parsed into MVP alerts even if they appear in a broad provider response.

### EODHD Financial News

Endpoint: `GET /api/news`

Response shape: an array of news records.

```json
[
  {
    "date": "2026-05-16T09:15:00+00:00",
    "title": "Company announces product launch",
    "content": "Article body or summary from the provider.",
    "link": "https://example.com/article",
    "symbols": ["0700.HK"],
    "tags": ["Technology"],
    "sentiment": {
      "polarity": 0.27,
      "neg": 0.02,
      "neu": 0.81,
      "pos": 0.17
    }
  }
]
```

Normalization:

- Candidate catalyst source only; do not notify directly from raw news.
- Stable source event ID: `eodhd:news:{hash(link_or_title_date)}`.
- Require deterministic filters plus LLM classification before alert creation.

### HKEX New Listings and HKEXnews

No stable public JSON API is selected for day one. Treat HKEX and HKEXnews as public web sources with HTML/PDF document links, not as strongly typed API providers.

New Listing Information parsed fields:

- `updated_date`
- `board`, such as Main Board or GEM
- `stock_code`
- `stock_name`
- `document_type`, such as new listing announcement, prospectus, or allotment result
- `document_url`
- `source_page_url`

HKEXnews title/predefined document search parsed fields:

- `publish_datetime`
- `stock_code`
- `stock_name`
- `headline_category`
- `title`
- `document_url`
- `document_format`

Normalization:

- Store raw HTML or search response text as `raw_payload`.
- Store downloaded PDFs as source documents or links, depending on storage constraints.
- Stable source event ID: `hkex:{stock_code}:{document_type}:{document_url_hash}`.
- HK grey-market data is not covered by HKEX; keep those fields nullable unless a separate acceptable source is added.

### OpenDART Corporation Code

Endpoint: `GET https://engopendart.fss.or.kr/engapi/corpCode.json`

Access/cost: OpenDART requires an authentication key. The FSS terms state that services are free in principle, but usage limits apply and the FSS may change limits or charge for certain services with advance notice.

Response shape: an object with status metadata and a `list` array.

```json
{
  "status": "000",
  "message": "OK",
  "list": [
    {
      "corp_code": "00126380",
      "corp_name": "Samsung Electronics Co., Ltd.",
      "corp_eng_name": "Samsung Electronics Co., Ltd.",
      "stock_code": "005930",
      "modify_date": "20260515"
    }
  ]
}
```

Normalization:

- Join `stock_code` to EODHD `KO`/`KQ` company reference records where possible.
- Store `corp_code` on the company reference or alias table for disclosure lookups.

### OpenDART Disclosure Search

Endpoint: `GET https://engopendart.fss.or.kr/engapi/list.json`

Access/cost: Same OpenDART authentication key and free-in-principle terms as the corporation code API.

Response shape: an object with pagination metadata and a `list` array.

```json
{
  "status": "000",
  "message": "OK",
  "page_no": 1,
  "page_count": 100,
  "total_count": 1,
  "total_page": 1,
  "list": [
    {
      "corp_cls": "Y",
      "corp_name": "Samsung Electronics Co., Ltd.",
      "corp_code": "00126380",
      "stock_code": "005930",
      "report_nm": "Investor Relations",
      "rcept_no": "20260516000001",
      "flr_nm": "Samsung Electronics Co., Ltd.",
      "rcept_dt": "20260516",
      "rm": "S"
    }
  ]
}
```

Normalization:

- Event type depends on classified `report_nm`; examples include investor day, share issuance, major matters, and product/business development.
- Stable source event ID: `opendart:{rcept_no}`.
- Disclosure viewer URL: `https://englishdart.fss.or.kr/dsbh001/main.do?rcpNo={rcept_no}`.
- Treat non-`000` status values as source-level failures or empty results based on OpenDART's message code.

### SEC EDGAR Submissions

Endpoint: `GET https://data.sec.gov/submissions/CIK##########.json`

Scope: V2 source, not MVP/day-one. Keep the response contract here so the later official US filing catalyst adapter has a concrete target.

Response shape: an object with company metadata and compact columnar filing arrays under `filings.recent`.

```json
{
  "cik": "0000320193",
  "entityType": "operating",
  "sic": "3571",
  "sicDescription": "Electronic Computers",
  "name": "Apple Inc.",
  "tickers": ["AAPL"],
  "exchanges": ["Nasdaq"],
  "filings": {
    "recent": {
      "accessionNumber": ["0000320193-26-000001"],
      "filingDate": ["2026-05-01"],
      "reportDate": ["2026-03-31"],
      "acceptanceDateTime": ["20260501160100"],
      "form": ["10-Q"],
      "items": [""],
      "primaryDocument": ["aapl-20260331.htm"],
      "primaryDocDescription": ["10-Q"]
    }
  }
}
```

Normalization:

- Expand columnar arrays by index into one filing record per accession number.
- Stable source event ID: `sec:{cik}:{accessionNumber}`.
- Source URL: `https://www.sec.gov/Archives/edgar/data/{cik_without_leading_zeroes}/{accession_without_dashes}/{primaryDocument}`.
- Use declared User-Agent and respect SEC rate guidance.

### Telegram Bot API

Outgoing endpoint: `POST https://api.telegram.org/bot{token}/sendMessage`

Successful Bot API responses contain `ok: true` and a `result` object. Failed responses contain `ok: false`, `description`, and usually `error_code`.

```json
{
  "ok": true,
  "result": {
    "message_id": 123,
    "chat": {
      "id": 123456789,
      "type": "private"
    },
    "date": 1780000000,
    "text": "Catalyst alert text"
  }
}
```

Incoming webhook or polling updates contain an `update_id` and one optional update payload such as `message`.

```json
{
  "update_id": 987654321,
  "message": {
    "message_id": 1,
    "from": {
      "id": 123456789,
      "is_bot": false,
      "first_name": "Alice"
    },
    "chat": {
      "id": 123456789,
      "type": "private"
    },
    "date": 1780000000,
    "text": "/start"
  }
}
```

Normalization:

- Store sent `message_id`, `chat.id`, delivery status, and error payloads in `notifications`.
- Dedupe incoming updates by `update_id`.

### OpenAI Responses API

Endpoint: `POST https://api.openai.com/v1/responses`

Response shape: a `response` object with status, output items, and token usage.

```json
{
  "id": "resp_abc123",
  "object": "response",
  "created_at": 1780000000,
  "status": "completed",
  "model": "gpt-5.4-mini",
  "output": [
    {
      "type": "message",
      "role": "assistant",
      "content": [
        {
          "type": "output_text",
          "text": "{\"is_material\":true,\"event_type\":\"product_launch\",\"confidence\":0.82}"
        }
      ]
    }
  ],
  "usage": {
    "input_tokens": 1200,
    "output_tokens": 120
  }
}
```

Normalization:

- Request structured JSON output for catalyst classification.
- Persist `response.id`, `model`, `status`, `usage`, prompt version, and parsed classifier output.
- If `status` is not `completed`, store the error/incomplete details and skip notification.

### Contract Sources

- EODHD OpenAPI specification: https://github.com/EodHistoricalData/eodhd-openapi
- EODHD calendar API docs: https://eodhd.com/financial-apis/calendar-upcoming-earnings-ipos-and-splits
- EODHD financial news API docs: https://eodhd.com/financial-apis/stock-market-financial-news-api
- HKEX New Listing Information: https://www2.hkexnews.hk/New-Listings/New-Listing-Information/Main-Board?sc_lang=en
- OpenDART corporation code API: https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DE001&apiId=AE00004
- OpenDART disclosure search API: https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DE001&apiId=AE00001
- SEC EDGAR APIs: https://www.sec.gov/search-filings/edgar-application-programming-interfaces
- Telegram Bot API: https://core.telegram.org/bots/api
- OpenAI Responses API: https://platform.openai.com/docs/api-reference/responses

## Source Run Requirements

- Record start/end time.
- Record item counts.
- Record errors.
- Do not crash the whole run because one company/source fails.
- Use retries with backoff.
- Respect source rate limits.
- Store provider error bodies and status codes in `source_runs.last_error` without overwriting successful prior data.
- Treat source-specific empty-result responses as successful runs only when the provider status explicitly means no data.

## Company Reference Requirements

- Sync company reference data from EODHD exchange symbol lists for day-one markets.
- Required exchange coverage:
  - US
  - HK
  - KO
  - KQ
- Upsert companies by normalized `(exchange, symbol)`.
- Preserve raw provider payloads for later debugging and integration.
- Mark missing companies inactive only after repeated absence, not after one failed sync.
- Expose a search endpoint for the admin UI:
  - `GET /api/v1/companies/search?q=&country=&exchange=&limit=`

## Acceptance Criteria

- Each source can be run independently.
- Company reference sync can run independently from event sync.
- Search can return company results even before any company is tracked.
- Each source writes raw payloads before normalization.
- Normalization failures are logged and visible.
- Adding a new source does not require changing notification logic.

---

# Module PRD 9: Portability

## Summary

Design the standalone app so its event store, adapters, and notification pipeline can be reused by, or exported to, a larger market-data system.

## Design Rules

- Keep stable `source_event_id`.
- Keep deterministic `dedup_key`.
- Store `raw_payload`.
- Avoid hardcoding single-user assumptions deep in adapters.
- Keep business logic separate from FastAPI routes.
- Use repository pattern.
- Keep async DB access.
- Use structlog.

## Acceptance Criteria

- Tracked companies and events can be exported.
- Source adapters can be reused in another codebase with minimal changes.
- Event schema stays source-agnostic (stable IDs, raw payload, normalized fields).

## 8. Release Plan

### Version 0: Scaffold

- FastAPI app
- Next.js admin UI shell
- Postgres, Redis, Celery
- Auth and seeded admin
- Basic settings

### Version 1: Calendar Alerts

- Company reference universe sync
- Company tracking
- EODHD earnings ingestion
- EODHD IPO ingestion for US/HK
- Telegram setup
- Notification dedup
- `/today`, `/thisweek`, `/earnings`, `/ipo`

### Version 2: Official Filing Depth

- HKEX new listings and announcements
- OpenDART disclosure search for Korean company catalysts
- SEC EDGAR submissions for US filing catalysts
- HK/Korea source health in UI
- HK IPO, Korea catalyst, and US filing normalization for those sources

### Version 3: Catalyst Classifier

- EODHD/company-news ingestion
- LLM classification
- Catalyst event creation
- Catalyst Telegram alerts

### Version 4: Review and Quality

- Notification review UI
- LLM classification logs
- False positive/negative labels
- Prompt and threshold tuning

## Open Questions

- Which sectors should be available in IPO filters on day one: exchange sectors, custom themes, or both?
- Should Telegram `/add TICKER` auto-resolve company metadata, or should metadata entry be web-only at first?
- For HK grey-market/subscription data, which source is acceptable and stable enough?
- Should removed-company data deletion be hard delete or soft delete by default?
- Should earnings alerts be sent once per event or at multiple configurable reminder windows?

## Non-Goals

- Multi-user collaboration.
- Team accounts.
- Automated trading.
- Full portfolio/PnL analytics.
- Broad trend discovery.
- LangGraph multi-agent research.
- Financial Modeling Prep backup integration.
- Korean IPO monitoring in the MVP.
