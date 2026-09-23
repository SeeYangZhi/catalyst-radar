"""HK IPO discovery via AAStocks (Phase 9.5a).

EODHD has no forward HK IPO coverage, so upcoming HK listings come from
the AAStocks IPO calendar (headless-rendered). Events are keyed by
stock code (`hkex:ipo:<code>`) so AAStocks / HKEX / EODHD HK rows for
the same listing dedupe onto one Event. Reuses the EODHD IPO gate and
reminder-window logic so HK alerts flow through the same path as US.
"""

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.adapters.aastocks_ipo import AastocksIpoAdapter
from catalyst_radar.dedup import dedup_key as make_dedup_key
from catalyst_radar.logging import get_logger
from catalyst_radar.models.base import today_local, utcnow
from catalyst_radar.models.event import Event, EventRelevance
from catalyst_radar.models.notification import Notification
from catalyst_radar.repositories.event_repository import EventRepository
from catalyst_radar.repositories.notification_repository import (
    NotificationRepository,
)
from catalyst_radar.repositories.source_repository import (
    RawItemRepository,
    SourceRunRepository,
)
from catalyst_radar.services.alerts import format_ipo
from catalyst_radar.services.ipo_sync import (
    _deal_size_usd,
    _enabled_countries,
    _etf_trust_indicators,
    _exchange_allowed,
    _industry_matches,
    _ipo_windows,
    _pick_window,
    country_for_exchange,
)

log = get_logger(__name__)


@dataclass(slots=True)
class HkIpoSyncSummary:
    fetched: int
    events_created: int
    matched: int
    notifications_created: int


async def sync_hk_ipos(
    session: AsyncSession,
    adapter: AastocksIpoAdapter | None = None,
) -> HkIpoSyncSummary:
    """Pull the AAStocks HK IPO calendar, upsert events, and create
    reminder-window notifications for enabled countries.

    Ordering contract: callers must run ``hk_ipo_enrich.flip_listed_hk_ipos``
    first when the HKEX newly-listed feed is enabled
    (``hkex_listed_flip_enabled``) — this function only skips events already
    flipped to ``status == "listed"``, so without a preceding flip a
    just-listed IPO can still generate a day-of reminder. See
    ``tasks.task_sync_hk_ipos`` for the canonical flip-before-sync chain.
    """
    from catalyst_radar.runtime_config import effective

    eff = await effective(session)
    if not bool(eff.hk_ipo_source_aastocks_enabled):
        log.info("hk_ipo_sync_disabled")
        return HkIpoSyncSummary(0, 0, 0, 0)

    adapter = adapter or AastocksIpoAdapter()
    runs = SourceRunRepository(session)
    run = await runs.start("aastocks.ipocalendar")

    try:
        result = await adapter.fetch()
    except Exception as exc:  # noqa: BLE001
        await runs.finish(run, status="failed", last_error=repr(exc))
        log.warning("hk_ipo_sync_failed", error=repr(exc))
        return HkIpoSyncSummary(0, 0, 0, 0)

    await RawItemRepository(session).store(
        source_name=result.source_name,
        schema_name=result.schema_name,
        raw_payload=result.payload,
        source_url=result.source_url,
        http_status=result.http_status,
        source_run_id=run.id,
    )
    if result.http_status != 200:
        await runs.finish(
            run,
            status="failed" if result.http_status else "empty",
            last_error=f"http {result.http_status}",
        )
        await session.commit()
        return HkIpoSyncSummary(0, 0, 0, 0)

    enabled = _enabled_countries(eff.eodhd_ipo_enabled_countries)
    events_repo = EventRepository(session)
    notif_repo = NotificationRepository(session)
    windows = _ipo_windows(eff.ipo_alert_windows_days)
    today = today_local(eff.alert_timezone)

    # New filter configs (shared with US IPO sync)
    exclude_etfs = eff.ipo_exclude_etfs_trusts
    industry_kw = [k.strip().lower() for k in eff.ipo_industry_keywords.split(",") if k.strip()]
    min_deal_size = eff.ipo_min_deal_size_usd
    exchange_filter = _enabled_countries(eff.ipo_exchange_filter)

    events_created = matched = notifications_created = 0
    filtered_out = {"etf": 0, "industry": 0, "deal_size": 0, "exchange": 0}

    for raw in result.items:
        norm = adapter.normalize(raw)
        country = country_for_exchange(norm["exchange"])
        sid = adapter.source_event_id(raw)
        event = Event(
            event_type="ipo",
            source_name=adapter.source_name,
            source_event_id=sid,
            dedup_key=adapter.dedup_key(raw),
            symbol=norm["symbol"],
            exchange=norm["exchange"],
            country=country,
            company_name=norm["company_name"],
            title=norm["title"],
            event_date=norm["event_date"],
            payload=norm["payload"],
        )
        event, created = await events_repo.upsert(event)
        if created:
            events_created += 1

        if country is None or country not in enabled:
            continue

        # Trading already started (Phase 9.5b flip) — the lifecycle is
        # closed, so no relevance refresh and no further reminders.
        if event.status == "listed":
            continue

        # --- New filters ---
        # 1. ETF/Trust exclusion
        if exclude_etfs and _etf_trust_indicators(event.company_name):
            filtered_out["etf"] += 1
            continue

        # 2. Exchange filter
        if not _exchange_allowed(event.exchange, exchange_filter):
            filtered_out["exchange"] += 1
            continue

        # 3. Industry keyword filter (matches company name or description)
        if industry_kw:
            desc = ((event.payload or {}).get("profile") or {}).get("description") or ""
            haystack = f"{event.company_name or ''} {desc}".lower()
            if not _industry_matches(haystack, industry_kw):
                filtered_out["industry"] += 1
                continue

        # 4. Minimum deal size
        if min_deal_size > 0:
            deal_size = _deal_size_usd(event.payload or {})
            if deal_size > 0 and deal_size < min_deal_size:
                filtered_out["deal_size"] += 1
                continue

        matched += 1
        session.add(
            EventRelevance(
                event_id=event.id,
                matched=True,
                reason=f"HK IPO via AAStocks ({country} enabled)",
            )
        )

        if event.event_date is None:
            continue
        days_until = (event.event_date.date() - today).days
        window = _pick_window(days_until, windows)
        if window is None:
            continue

        dkey = make_dedup_key(sid, "ipo", str(window))
        if await notif_repo.get_by_dedup_key(dkey) is not None:
            continue
        session.add(
            Notification(
                event_id=event.id,
                channel="telegram",
                dedup_key=dkey,
                reminder_window=str(window),
                status="pending",
                payload={"text": format_ipo(event)},
                dispatch_after=utcnow() + timedelta(minutes=int(eff.ipo_dispatch_grace_minutes)),
            )
        )
        notifications_created += 1

    await session.commit()
    await runs.finish(run, status="success", item_count=len(result.items))
    await session.commit()
    log.info(
        "hk_ipo_sync_ok",
        fetched=len(result.items),
        events_created=events_created,
        matched=matched,
        notifications=notifications_created,
        filtered=filtered_out,
    )
    return HkIpoSyncSummary(
        fetched=len(result.items),
        events_created=events_created,
        matched=matched,
        notifications_created=notifications_created,
    )
