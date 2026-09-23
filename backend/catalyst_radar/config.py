from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Single canonical source of defaults for every behavioural knob.

    Layering:
      1. `.env` / env vars override defaults — used only for secrets and
         deployment-specific values (DB URLs, ports, API keys, etc.).
         See `.env.example` for the contract.
      2. This class holds the defaults for everything else, including
         all UI-tunable behavioural keys.
      3. The `app_config` DB table stores user-edited overrides for the
         subset of keys listed in `runtime_config.EDITABLE`.
      4. Behavioural code reads via `runtime_config.effective(session)`
         so UI edits take effect without a redeploy. Boot-time code
         (Celery Beat) reads via `celery_app._boot_override` for the
         "restart required" keys.

    Direct `settings.X` reads are only correct for non-EDITABLE keys
    (secrets, infra, immutable constants)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── App ───────────────────────────────────────────────────────────
    app_name: str = "Catalyst Radar"
    environment: str = "development"
    debug: bool = True
    log_level: str = "info"

    # ── API ───────────────────────────────────────────────────────────
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"
    cors_origin_regex: str = r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"

    # ── PostgreSQL ────────────────────────────────────────────────────
    database_url: str = "postgresql+asyncpg://radar_user:radar_pass@localhost:5432/radar_db"
    database_url_sync: str = "postgresql://radar_user:radar_pass@localhost:5432/radar_db"
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_pool_timeout: int = 30

    # ── Redis / Celery ────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/0"

    # ── Authentication ────────────────────────────────────────────────
    jwt_secret_key: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 10080
    default_admin_email: str = "admin@radar.local"
    default_admin_password: str = "admin123!"
    registration_enabled: bool = False

    # ── OpenAI ────────────────────────────────────────────────────────
    openai_api_key: str = ""
    catalyst_classifier_model: str = "gpt-5.4-mini"
    catalyst_classifier_max_output_tokens: int = 1200
    catalyst_classifier_temperature: float = 0
    openai_timeout_seconds: int = 90
    openai_max_retries: int = 2
    # v3: drop dates from story_key + controlled-vocab slugs 
    catalyst_prompt_version: str = "v3"
    catalyst_min_importance: str = "medium"
    catalyst_min_confidence: float = 0.6
    catalyst_autosend_min_importance: str = "high"
    catalyst_autosend_min_confidence: float = 0.75
    catalyst_max_items_per_run: int = 50
    # Drop news items dated older than this before classification — saves
    # an LLM call and prevents stale-news alerts (web_search occasionally
    # surfaces months-old articles for thinly-covered names; EODHD's news
    # API has also been seen to backfill old items into the recent feed).
    catalyst_news_max_age_days: int = 21
    # Drop news items with no ``date`` (the stale-news gate above only fires
    # when a date is present, so static product pages with no in-page date
    # used to slip through and get alerted as fresh launches months after
    # they actually went live — Unitree H2/R1/DigitalServo). A real news
    # article carries a date; web_search sets ``published_date=null`` for
    # the rediscovery-of-old-product-page case.
    catalyst_drop_undated_news: bool = True
    # Per-ticker CN news via akshare's Eastmoney wrapper (stock_news_em).
    # Runs inside sync_catalysts for tracked SSE/SZSE/BSE listings, between
    # the EODHD and web_search sweeps. Every item carries a publish datetime,
    # so this is the *dated* CN path that lets the web_search sweep step back
    # from rediscovering undated CN product pages.
    catalyst_eastmoney_news_enabled: bool = True
    # Taiwan material-information announcements via MOPS (公開資訊觀測站,
    # mops.twse.com.tw) — the authoritative primary disclosure source for
    # TWSE / TPEx listings. Runs inside sync_catalysts for tracked
    # TW/TWO/TWSE/TPEX names, between the Eastmoney and web_search sweeps.
    # Closes the Taiwan news gap (EODHD /news returns [] for TW).
    catalyst_mops_news_enabled: bool = True
    # EODHD's /news endpoint returns nothing for these exchanges (Taiwan and
    # mainland China — verified live), yet each call still costs 5 EODHD API
    # units (news is weight-5, the dominant quota consumer). Skip the call
    # entirely for them: Taiwan is served by the web_search gap sweep and
    # CN-listed by the Eastmoney sweep, so this is pure quota savings with no
    # coverage loss. Empty to disable the skip. (quota analysis.)
    catalyst_eodhd_news_skip_exchanges: str = "TW,TWO,TPEX,SSE,SZSE,BSE"

    # ── Catalyst enrichment (PDF/HTML structured extraction) ─────────
    # When the classifier tags an item as an earnings report or guidance
    # change, fetch the cited URL, extract the document text (PDF via
    # pypdf, HTML via tag strip), and run a second LLM call with a strict
    # JSON schema to pull revenue/net-profit/EPS/YoY/key drivers into
    # ``payload.financials``. Bounded per run so a burst of filings can't
    # blow the LLM budget.
    catalyst_enrich_earnings_enabled: bool = True
    catalyst_enrich_max_per_run: int = 10
    catalyst_enrich_model: str = "gpt-5.4-mini"
    catalyst_enrich_max_chars: int = 25000
    catalyst_enrich_fetch_timeout_seconds: int = 60
    catalyst_enrich_max_output_tokens: int = 3000
    # Analyst-consensus search (separate web_search call after extraction).
    # Web_search latency runs 30-90s, so we use a longer timeout than the
    # default openai_timeout. A failed consensus call never blocks the
    # alert — only the actuals + YoY render.
    catalyst_enrich_consensus_enabled: bool = True
    catalyst_enrich_consensus_timeout_seconds: int = 180
    catalyst_enrich_consensus_max_output_tokens: int = 6000

    # ── Company sources & discovery  ────────────────────────
    # Master switch for the blog/IR primary-source pipeline (Phase B).
    catalyst_blog_sources_enabled: bool = False
    # Auto-run source discovery when a company is tracked (Phase D).
    catalyst_discovery_enabled: bool = False
    # Discovery backend: OpenAI Responses web_search.
    catalyst_discovery_model: str = "gpt-5.4-mini"
    catalyst_discovery_max_output_tokens: int = 2000
    # Web_search catalyst sweep (runs inside sync_catalysts).
    catalyst_websearch_enabled: bool = False
    # When true, sweep every tracked company (relies on cross-source dedup);
    # when false, only the gap exchanges below.
    catalyst_websearch_all_markets: bool = True
    # even when the master websearch switch is off, keep sweeping the
    # gap exchanges below — web_search is their *only* news source (EODHD's
    # /news is empty for Taiwan and CN pre-IPO names). Off → those names get
    # no catalyst news at all until the master switch is enabled.
    catalyst_websearch_gap_always_on: bool = True
    # Keep this a superset of catalyst_eodhd_news_skip_exchanges so every
    # exchange we skip from EODHD has web_search as a fallback even when the
    # Eastmoney sweep is disabled (BSE in particular). Eastmoney-covered names
    # are still skipped here while it's enabled, so the extra entries cost
    # nothing in the default config — they only matter as the fallback.
    catalyst_websearch_gap_exchanges: str = "TW,TWO,TPEX,SSE,SZSE,BSE"
    catalyst_websearch_model: str = "gpt-5.4-mini"
    catalyst_websearch_lookback_days: int = 7
    catalyst_websearch_max_items: int = 10

    # Cross-source dedup (Phase C). URL-exact is always on; these
    # tune the semantic same-story merge layered on top.
    catalyst_dedup_window_hours: int = 72
    catalyst_dedup_title_threshold: int = 88  # >= this similarity → merge
    # token_set_ratio drifts on paraphrased news summaries (real-world same-event
    # CXMT/Unitree clusters scored 45-70 across outlets), so a tight 75 grey
    # bar meant the LLM judge never fired and duplicates leaked through. 55
    # admits real duplicates into the grey zone; the LLM judge is the actual
    # decider and still gates false-positive merges.
    catalyst_dedup_grey_threshold: int = 55  # [grey, title) → ask the LLM judge
    catalyst_dedup_llm_judge_enabled: bool = True
    # story_key fast-path proximity guard (Layer-2). Even when the
    # canonical slug matches (e.g. partnership_nvda), require event_date
    # to be within this many days of the candidate's event_date — else
    # treat as a genuinely-new event. Prevents over-collapse when the
    # same counterparty announces multiple distinct deals within the
    # 72h dedup window. Missing event_date on either side falls through
    # to merge (conservative: trust the canonical key when we have no
    # date signal to disprove it).
    catalyst_story_key_max_date_gap_days: int = 7
    # Repeat-alert window (step 3). A second autosend-grade catalyst
    # for the same (symbol, subtype) within this many hours of an
    # already-notified one is demoted to review — Telegram stays quiet,
    # the dashboard still shows it. Exempt when the two event_dates are
    # more than catalyst_story_key_max_date_gap_days apart (clearly
    # distinct events, e.g. two different deals weeks apart). 0 disables.
    catalyst_repeat_alert_window_hours: int = 24

    # ── EODHD ─────────────────────────────────────────────────────────
    eodhd_api_key: str = ""
    eodhd_base_url: str = "https://eodhd.com/api"
    eodhd_company_reference_exchanges: str = "US,HK,KO,KQ,TW"
    # Master switch for the daily exchange-symbol-list sync. HTTP 402
    # responses here mean the daily EODHD request quota is exhausted —
    # subsequent runs recover automatically. Flip this off via the
    # Settings UI to skip the sync entirely (e.g. to conserve quota for
    # other endpoints). Existing reference data continues to drive
    # ticker matching regardless of this flag.
    eodhd_company_reference_enabled: bool = True
    eodhd_ipo_enabled_countries: str = "US,HK,CN"
    eodhd_earnings_lookahead_days: int = 45
    eodhd_ipo_lookahead_days: int = 90
    # First-send description coverage (see specs/2026-06-18-ipo-description-two-tier).
    # Newly-minted IPO notifications wait this long before any dispatcher may
    # send them, giving the inline enrich a window to attach a description; the
    # enrich pulls this forward for events it describes, so the good case has no
    # added delay. Un-describable events send when the grace elapses and
    # self-heal via alert_backfill later.
    ipo_dispatch_grace_minutes: int = 5
    # Kill-switch for the fast OpenAI web_search provisional blurb (imminent
    # events only). When off, enrichment is prospectus-first as before.
    ipo_websearch_provisional_enabled: bool = True
    eodhd_request_timeout_seconds: int = 30
    # Cadence for EODHD calendar polls (earnings + IPO) and EDGAR/HKEX
    # enrichments. Default 6h ≈ morning / midday / afternoon / overnight
    # coverage against once-daily upstream refresh.
    eodhd_sync_interval_minutes: int = 360
    # Cadence for catalyst news polling. Separated from the calendar
    # interval so users can tighten alert latency without amplifying
    # calendar-poll volume. Default 6h matches the old behaviour.
    catalyst_sync_interval_minutes: int = 360

    # ── HKEX ──────────────────────────────────────────────────────────
    hkex_new_listings_url: str = (
        "https://www2.hkexnews.hk/New-Listings/New-Listing-Information/Main-Board?sc_lang=en"
    )
    hkexnews_base_url: str = "https://www1.hkexnews.hk"
    hkex_request_timeout_seconds: int = 30
    hkex_sync_interval_minutes: int = 360

    # ── AAStocks (HK IPO discovery, headless-rendered) ────────────────
    aastocks_ipo_calendar_url: str = "http://www.aastocks.com/en/stocks/market/ipo/ipocalendar.aspx"
    hk_ipo_source_aastocks_enabled: bool = True
    aastocks_render_timeout_seconds: int = 45

    # ── HKEX newly-listed flip (Phase 9.5b) ──────────────────────────
    # Flip HK IPO events to status="listed" once trading starts,
    # confirmed by the per-year HKEXnews "New Listing Report" workbook
    # (a static xlsx linked from the New Listing Information page).
    hkex_listed_flip_enabled: bool = True
    # Main-Board-only report (".../Main/..."): GEM listings (08xxx codes)
    # never appear here, so they are never flipped to "listed" and keep
    # generating reminders. Known gap — GEM support not implemented.
    hkex_new_listing_report_url_template: str = (
        "https://www2.hkexnews.hk/-/media/HKEXnews/Homepage/New-Listings/"
        "New-Listing-Information/New-Listing-Report/Main/NLR{year}_Eng.xlsx"
    )

    # ── HKEXnews prospectus (HK IPO filings enrichment, Phase 9.6b) ───
    hkex_prospectus_enrich_enabled: bool = True
    hkex_prospectus_max_items_per_run: int = 5
    hkex_prospectus_download_timeout: int = 240

    # ── Tushare (A-share share-unlock schedule via share_float) ──────
    # 120-credit free tier suffices. Token is account-bound; store in
    # backend/.env as TUSHARE_API_TOKEN and never commit it.
    tushare_api_token: str = ""
    cn_unlocks_enabled: bool = True
    # Sync cadence — share_float is forward-looking + rarely amended, so
    # a daily pull is plenty AND lets T-3/T-1 reminder windows fire on
    # time. Hourly would just rack up API calls without adding signal.
    cn_unlocks_sync_interval_minutes: int = 1440
    # Look-ahead window: only create catalyst events for unlocks landing
    # within the next N days. 30 covers all five reminder windows
    # (T-30/T-14/T-7/T-3/T-1) with comfortable headroom.
    cn_unlocks_lookahead_days: int = 30
    # Per-call HTTP timeout. tushare's API is normally < 1 s but
    # occasionally stalls on backend cache misses.
    tushare_request_timeout_seconds: int = 30
    # Importance-from-float-ratio thresholds. Float ratio is the %
    # of total share count unlocking on a given date. A 5%+ unlock can
    # move the price; a < 1% unlock is noise.
    cn_unlocks_high_ratio_pct: float = 5.0
    cn_unlocks_medium_ratio_pct: float = 1.0
    # Five-step reminder schedule: T-30 / T-14 / T-7 / T-3 / T-1.
    unlock_alert_windows_days: str = "30,14,7,3,1"

    # ── Mainland China (SSE + SZSE) IPO sources ──────────────────────
    # akshare wraps Eastmoney's stock_xgsglb_em (upcoming/recent A-share
    # IPOs with subscription/listing/price). Free, public, no auth.
    cn_ipo_source_akshare_enabled: bool = True
    cn_ipo_sync_interval_minutes: int = 30
    # CNINFO 巨潮资讯网 — CSRC-designated official disclosure portal.
    # Free, public, no auth at <1 req/s. Prospectus PDFs are large
    # (typical A-share prospectus is 200-800 pages, 5-30 MB); we run a
    # bounded section-aware extractor downstream rather than truncating.
    cninfo_request_timeout_seconds: int = 30
    cninfo_pdf_download_timeout: int = 240
    cn_ipo_prospectus_enrich_enabled: bool = True
    cn_ipo_prospectus_max_items_per_run: int = 5
    # CSRC IPO review-committee hearings (akshare.stock_ipo_review_em).
    # Catches the 上会 / 上会通过 / 上会未通过 events — the only signal
    # for pre-listing names like CXMT or Unitree, whose 6-digit ticker
    # has not been assigned yet. The endpoint is slow (~80 s, 11 paged
    # requests, 5k+ historical rows), so the default cadence is loose.
    cn_ipo_review_enabled: bool = True
    cn_ipo_review_sync_interval_minutes: int = 360

    # ── SEC EDGAR (US IPO filings enrichment, Phase 9.6a) ─────────────
    sec_edgar_user_agent: str = "Catalyst Radar admin@radar.local"
    # Honest, descriptive User-Agent for the public-web scrapers (HKEX,
    # CNINFO, MOPS). Identify your deployment and include a contact URL.
    scraper_user_agent: str = "CatalystRadar/0.1 (+https://github.com/SeeYangZhi/catalyst-radar)"
    sec_edgar_request_timeout_seconds: int = 30
    sec_edgar_enrich_enabled: bool = True
    sec_edgar_max_items_per_run: int = 10

    # ── Telegram ──────────────────────────────────────────────────────
    telegram_bot_token: str = ""
    # Comma-separated admin chat ids: may flip /settings toggles and receive
    # ops/monitoring alerts. Empty = every chat is admin (dev only).
    telegram_admin_chat_id: str = ""
    # False (default): only admin chats may /start and use the bot. True:
    # anyone who finds the bot can /start, receive alerts and keep their own
    # stars/dismissals; the watchlist + alert stream become visible to them.
    telegram_open_subscribe: bool = False
    telegram_parse_mode: str = "HTML"
    telegram_alerts_enabled: bool = True
    telegram_polling_enabled: bool = True
    telegram_webhook_url: str = ""
    telegram_webhook_secret: str = ""
    telegram_request_timeout_seconds: int = 30

    # ── Alert Scheduling ──────────────────────────────────────────────
    alert_timezone: str = "UTC"
    earnings_alert_windows_days: str = "7,1,0"
    ipo_alert_windows_days: str = "14,7,1"
    daily_digest_hour: int = 8
    weekly_digest_day: str = "MONDAY"
    weekly_digest_hour: int = 8
    ipo_exclude_etfs_trusts: bool = False
    ipo_industry_keywords: str = ""
    ipo_min_deal_size_usd: int = 0
    ipo_exchange_filter: str = ""

    # ── Self-monitoring (alerting on the alerter) ────────────────────
    # A beat watchdog (catalyst_radar.health_monitor) pings Telegram when
    # ingestion silently stalls or a provider quota trips — the failure
    # modes an operator would otherwise catch only by hand.
    monitor_self_enabled: bool = True
    # Alert when no source_run has SUCCEEDED in this many hours. Default 12h
    # sits comfortably above the 6h default calendar/news cadence, so a
    # genuine stall (every source down) fires without false positives. Tune
    # up if you loosen the sync intervals.
    monitor_max_ingestion_gap_hours: int = 12
    # Per-source stall threshold, decoupled from the global gap above. A single
    # source is flagged "stalled" only when it hasn't SUCCEEDED in this many
    # hours — wider than the 12h gap so a slow 6h-cadence source (e.g.
    # akshare.stock_ipo_review_em) riding out one transient ChunkedEncodingError
    # plus a missed tick (~12h between successes) doesn't false-alarm. A
    # genuinely dead source still fires once it crosses this; 18h ≈ 3 missed 6h
    # cycles. The global "everything is down" guard still uses the 12h gap.
    monitor_source_stale_hours: int = 18
    # A persistent condition re-alerts at most once per this window (so a
    # day-long EODHD quota outage pings once, not every 30-min tick).
    monitor_alert_cooldown_hours: int = 6
    # Earlier "source instability" tier: a source that keeps FAILING but
    # recovers before the stale threshold never trips the stall alert. We
    # flag it when it racks up >= threshold failed runs within this window —
    # the window is wider than the 12h gap so a slow-burn flap is caught.
    monitor_failure_streak_window_hours: int = 24
    monitor_failure_streak_threshold: int = 4
    # Retention: a daily task drops source_runs (and the raw_items that FK
    # them) older than this. The append-only audit log otherwise grows ~200
    # rows/day forever; the watchdog only reads ~24h and the Source Runs page
    # a handful of days, so 30 days is ample. Clamped to >= 2 days at runtime
    # so a misconfig can't starve the watchdog of history.
    source_runs_retention_days: int = 30
    # Optional Sentry error tracking. No-op unless a DSN is set AND
    # sentry-sdk is installed (the optional "monitoring" extra). Keeps the
    # base image lean while making real error capture one env var away.
    sentry_dsn: str = ""
    sentry_traces_sample_rate: float = 0.0

    # ── Entity relationships / spillover  ──────────────────
    # Master switch for the catalyst-spillover pipeline. When off, the
    # affected-companies pass is skipped; alerts behave like pre-#6.
    relationship_propagation_enabled: bool = True
    # Run preflight (LLM entity resolution + relationship suggestions)
    # whenever a tracked company is added via the API. Suggestions land
    # in the relationship_suggestions queue for user review.
    relationship_preflight_enabled: bool = True
    relationship_preflight_model: str = "gpt-5.4-mini"
    # Per-event LLM assessment that decides which BFS candidates are
    # materially impacted and assigns importance + reason.
    relationship_assessment_model: str = "gpt-5.4-mini"
    relationship_assessment_max_output_tokens: int = 4000
    # BFS hop cap. 2 hops covers parent + sibling (via shared parent) +
    # JV-partner; 1 would lose siblings; 3 lets the candidate set explode.
    relationship_max_hops: int = 2

    @property
    def cors_origins_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    @property
    def telegram_admin_chat_ids(self) -> frozenset[str]:
        return frozenset(
            item.strip() for item in self.telegram_admin_chat_id.split(",") if item.strip()
        )

    @property
    def is_production(self) -> bool:
        return self.environment.strip().lower() in {"production", "prod"}

    def production_secret_problems(self) -> list[str]:
        """Insecure shipped defaults that must be overridden before a
        production deploy. Empty outside production (dev/test keep the
        convenient defaults). Enforced at app startup in ``main.lifespan``
        so a misconfigured prod boot fails fast instead of silently signing
        JWTs with a public placeholder key."""
        if not self.is_production:
            return []
        problems: list[str] = []
        if self.jwt_secret_key.strip() in {"", "change-me-in-production"}:
            problems.append("JWT_SECRET_KEY is unset or still the default placeholder")
        if self.default_admin_password == "admin123!":
            problems.append("DEFAULT_ADMIN_PASSWORD is still the insecure default")
        if ":radar_pass@" in self.database_url:
            problems.append("DATABASE_URL still uses the default Postgres password")
        if self.telegram_bot_token and not self.telegram_admin_chat_ids:
            problems.append(
                "TELEGRAM_ADMIN_CHAT_ID is empty, which makes every Telegram chat an admin"
            )
        return problems


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
