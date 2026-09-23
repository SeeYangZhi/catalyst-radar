import asyncio
from datetime import timedelta

from celery import Celery
from celery.schedules import crontab
from celery.signals import worker_process_init

from catalyst_radar.config import settings
from catalyst_radar.logging import get_logger

log = get_logger(__name__)

celery_app = Celery(
    "catalyst_radar",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["catalyst_radar.tasks"],
)


def _boot_override(key: str, fallback, cast):
    """Honour the app_config DB override for a boot-time key.

    The UI marks these keys "restart required" because Celery Beat reads
    them once at startup; without this lookup, UI edits would silently
    no-op until someone also edited the env. Falls back to the
    env-derived Settings value if the DB is unreachable or if we're
    imported inside a running event loop (i.e. lazy-imported from the
    FastAPI process — that path doesn't need a fresh boot lookup
    anyway)."""
    try:
        asyncio.get_running_loop()
        return fallback  # imported under a live loop (FastAPI) — skip lookup
    except RuntimeError:
        pass  # no running loop → safe to asyncio.run() below

    try:
        from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

        from catalyst_radar.repositories.config_repository import (
            ConfigRepository,
        )

        async def _fetch():
            engine = create_async_engine(
                settings.database_url,
                connect_args={"statement_cache_size": 0},
            )
            try:
                async with AsyncSession(engine, expire_on_commit=False) as s:
                    value = await ConfigRepository(s).get(key)
            finally:
                await engine.dispose()
            if value is None:
                return None
            try:
                return cast(value)
            except (TypeError, ValueError):
                return None

        override = asyncio.run(_fetch())
    except Exception as exc:  # noqa: BLE001 - boot-time best effort
        log.warning("boot_override_lookup_failed", key=key, error=repr(exc))
        return fallback
    return override if override is not None else fallback


# Cadence philosophy:
#   - Reference data (exchange listings) is essentially static → weekly.
#   - Calendars (earnings, IPOs) refresh daily upstream → 6h on the
#     market-day side gives "morning + midday + afternoon + overnight"
#     coverage without spamming.
#   - News for tracked companies is the time-sensitive path → its own
#     knob (catalyst_sync_interval_minutes) so users can tighten it
#     without affecting the slower calendar polls.
#   - Enrichments (EDGAR/HKEX prospectus) are bounded by per-run budget
#     anyway → match the calendar cadence so newly created events are
#     enriched on the same beat tick.
def _every(minutes: int) -> timedelta:
    return timedelta(minutes=max(1, minutes))


_eodhd_minutes = _boot_override(
    "eodhd_sync_interval_minutes", settings.eodhd_sync_interval_minutes, int
)
_catalyst_minutes = _boot_override(
    "catalyst_sync_interval_minutes", settings.catalyst_sync_interval_minutes, int
)
_hkex_minutes = _boot_override(
    "hkex_sync_interval_minutes", settings.hkex_sync_interval_minutes, int
)
_cn_ipo_minutes = _boot_override(
    "cn_ipo_sync_interval_minutes", settings.cn_ipo_sync_interval_minutes, int
)
_cn_unlocks_minutes = _boot_override(
    "cn_unlocks_sync_interval_minutes", settings.cn_unlocks_sync_interval_minutes, int
)
_cn_ipo_review_minutes = _boot_override(
    "cn_ipo_review_sync_interval_minutes",
    settings.cn_ipo_review_sync_interval_minutes,
    int,
)
_alert_timezone = _boot_override(
    "alert_timezone", settings.alert_timezone, str
)
log.info(
    "beat_schedule_resolved",
    eodhd_minutes=_eodhd_minutes,
    catalyst_minutes=_catalyst_minutes,
    hkex_minutes=_hkex_minutes,
    cn_ipo_minutes=_cn_ipo_minutes,
    alert_timezone=_alert_timezone,
)


celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone=_alert_timezone,
    enable_utc=True,
    task_track_started=True,
    # Hard ceiling: web_search enrichment can take 30-90s per LLM call,
    # the consensus pass can stack two; a CNINFO pull plus PDF parses adds
    # more. 1h gives 4-5x headroom; the soft limit gives the task a chance
    # to commit partial work and log before being killed.
    task_time_limit=3600,
    task_soft_time_limit=3300,
    beat_schedule={
        # Reference data: weekly Sunday 02:00 UTC. Exchange symbol lists
        # change at the speed of corporate actions, not hours.
        "company-reference-weekly": {
            "task": "catalyst_radar.sync_company_reference",
            "schedule": crontab(day_of_week=0, hour=2, minute=0),
        },
        "earnings-sync": {
            "task": "catalyst_radar.sync_earnings",
            "schedule": _every(_eodhd_minutes),
        },
        "ipo-sync": {
            "task": "catalyst_radar.sync_ipos",
            "schedule": _every(_eodhd_minutes),
        },
        "hk-ipo-sync": {
            "task": "catalyst_radar.sync_hk_ipos",
            "schedule": _every(_hkex_minutes),
        },
        "edgar-enrich": {
            "task": "catalyst_radar.enrich_us_ipos",
            "schedule": _every(_eodhd_minutes),
        },
        "hkex-enrich": {
            "task": "catalyst_radar.enrich_hk_ipos",
            "schedule": _every(_hkex_minutes),
        },
        "cn-ipo-sync": {
            "task": "catalyst_radar.sync_cn_ipos",
            "schedule": _every(_cn_ipo_minutes),
        },
        "cn-ipo-enrich": {
            "task": "catalyst_radar.enrich_cn_ipos",
            "schedule": _every(_cn_ipo_minutes),
        },
        "cn-unlocks-sync": {
            "task": "catalyst_radar.sync_cn_unlocks",
            "schedule": _every(_cn_unlocks_minutes),
        },
        # CSRC review-stage IPO calendar — slow endpoint (~80s), so the
        # default cadence is loose (6h). Catches 上会通过 / 上会未通过
        # transitions for pre-listing names without 6-digit tickers.
        "cn-ipo-review-sync": {
            "task": "catalyst_radar.sync_cn_ipo_review",
            "schedule": _every(_cn_ipo_review_minutes),
        },
        # News polling — own knob so users can tighten alert latency
        # without changing the calendar pollers.
        "catalyst-sync": {
            "task": "catalyst_radar.sync_catalysts",
            "schedule": _every(_catalyst_minutes),
        },
        "dispatch-pending": {
            "task": "catalyst_radar.dispatch",
            "schedule": timedelta(minutes=5),
        },
        # Self-monitoring watchdog: alert on a stalled pipeline or a tripped
        # provider quota. 30-min tick; the task is cooldown-deduped so a
        # persistent condition pings once per monitor_alert_cooldown_hours.
        "health-monitor": {
            "task": "catalyst_radar.health_monitor",
            "schedule": timedelta(minutes=30),
        },
        "telegram-poll": {
            "task": "catalyst_radar.telegram_poll",
            "schedule": timedelta(seconds=20),
        },
        # Retention: prune source_runs + their raw_items past the window once
        # a day. 04:00 UTC keeps clear of the 03:30 host Postgres→GCS backup.
        "prune-source-runs": {
            "task": "catalyst_radar.prune_source_runs",
            "schedule": crontab(hour=4, minute=0),
        },
        # Hourly tick; the task self-gates on the user-configured local
        # daily_digest_hour / weekly_digest_day+hour and is idempotent per day/week.
        "digests": {
            "task": "catalyst_radar.digests",
            "schedule": crontab(minute=0),
        },
    },
)


@worker_process_init.connect
def _init_worker_error_tracking(**_kwargs: object) -> None:
    """Each worker process initialises its own Sentry SDK (no-op unless a
    DSN is configured + sentry-sdk installed), so background task failures
    are captured the same as API errors."""
    from catalyst_radar.observability import init_error_tracking

    init_error_tracking()


@celery_app.task(name="catalyst_radar.ping")
def ping() -> str:
    log.info("celery_ping")
    return "pong"
