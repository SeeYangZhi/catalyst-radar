# Decisions / PRD Deviations

Intentional departures from `docs/product-specs/PRD-catalyst-radar.md` and
other durable design decisions. Add an entry when a change deliberately
diverges from the PRD.

## Telegram access model

PRD Module 6 specifies delivery to the admin's configured chat only. The bot
supports that as the default and an opt-in open mode:

- **Closed (default, `TELEGRAM_OPEN_SUBSCRIBE=false`).** Only chats listed in
  `TELEGRAM_ADMIN_CHAT_ID` (comma-separated) can `/start` and use the bot. A
  self-hoster must set it to their own chat id. If it is left empty every chat
  is treated as admin, which is only acceptable while finding your chat id
  during first setup.
- **Open (`TELEGRAM_OPEN_SUBSCRIBE=true`).** Any chat can `/start`, receive
  every alert and digest, and use the browse commands.
- **Per-chat state.** Star / dismiss / listing-day-reminder state is keyed on
  `(event_id, chat_id)`. One chat's taps never change another chat's lists,
  buttons, reminders or digests.
- **Admin-only global actions.** Only admin chats can flip `/settings`
  toggles (global runtime config) and only admin chats receive ops /
  monitoring alerts.
- **Global content.** The watchlist and the alert stream are shared: in open
  mode every subscriber sees the configured watchlist and catalyst output.
  Enable open mode only if that is acceptable.

## Single admin account for the web dashboard

The dashboard has one seeded admin user (JWT auth, registration off by
default). There are no roles or multi-tenant separation; the Telegram side is
the only multi-recipient surface.

## Source substitutions

- **HK IPO discovery uses AAStocks, not EODHD.** EODHD's HK IPO calendar is
  effectively post-listing, so upcoming HK IPOs come from the AAStocks calendar,
  enriched and confirmed by HKEXnews. EODHD remains the US IPO source.
- **CN IPO coverage was added** (Eastmoney calendar via akshare, CSRC
  review-committee stage, CNINFO prospectuses, tushare share unlocks) although
  the original PRD scoped IPO monitoring to US / HK.
- **Taiwan news comes from MOPS.** EODHD `/news` returns nothing for TW listings;
  the MOPS material-information feed is the primary source, with OpenAI
  `web_search` as a supplement for TW and CN.
- **SEC EDGAR is used for US IPO prospectus enrichment** rather than filing
  catalysts (see the roadmap in `docs/architecture.md`).

## Honest scraper identity

The HTTP scrapers of public sites (HKEXnews, CNINFO, MOPS) identify themselves
with a descriptive `SCRAPER_USER_AGENT` rather than impersonating a browser. SEC
EDGAR requests use `SEC_EDGAR_USER_AGENT`, which should carry a contact
address per SEC fair-access guidance.

## Outbound URL safety

The URL-liveness prober and document downloaders refuse private, loopback and
link-local addresses, so a URL returned by a source or an LLM cannot be used to
reach internal services.
