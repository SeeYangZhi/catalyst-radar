"""Mainland China IPO discovery (SSE + SZSE).

Pulls upcoming/recent A-share IPOs from akshare's Eastmoney-backed
list, persists each as an Event keyed on the 6-digit ticker, and
schedules a Telegram alert via the shared `ipo_sync` filter+window
helpers. Mirrors `hk_ipo_sync`; differs only in the source adapter +
country code (CN).

CNINFO filings are pulled by a separate enrichment service
(`cn_ipo_enrich`) — the discovery sync is fast (single HTTP call,
~5 s), enrichment is slow (PDF download + LLM call per IPO) and runs
under its own per-run budget.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.adapters.akshare_ipo import AkshareIpoAdapter
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
class CnIpoSyncSummary:
    fetched: int
    events_created: int
    matched: int
    notifications_created: int


async def sync_cn_ipos(
    session: AsyncSession,
    adapter: AkshareIpoAdapter | None = None,
) -> CnIpoSyncSummary:
    from catalyst_radar.runtime_config import effective

    eff = await effective(session)
    if not bool(eff.cn_ipo_source_akshare_enabled):
        log.info("cn_ipo_sync_disabled")
        return CnIpoSyncSummary(0, 0, 0, 0)

    adapter = adapter or AkshareIpoAdapter()
    runs = SourceRunRepository(session)
    run = await runs.start("akshare.stock_xgsglb_em")

    try:
        result = await adapter.fetch()
    except Exception as exc:  # noqa: BLE001 - never let one bad sync abort
        await runs.finish(run, status="failed", last_error=repr(exc))
        log.warning("cn_ipo_sync_failed", error=repr(exc))
        return CnIpoSyncSummary(0, 0, 0, 0)

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
        return CnIpoSyncSummary(0, 0, 0, 0)

    enabled = _enabled_countries(eff.eodhd_ipo_enabled_countries)
    events_repo = EventRepository(session)
    notif_repo = NotificationRepository(session)
    windows = _ipo_windows(eff.ipo_alert_windows_days)
    today = today_local(eff.alert_timezone)

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

        if exclude_etfs and _etf_trust_indicators(event.company_name):
            filtered_out["etf"] += 1
            continue

        if not _exchange_allowed(event.exchange, exchange_filter):
            filtered_out["exchange"] += 1
            continue

        if industry_kw:
            desc = ((event.payload or {}).get("profile") or {}).get("description") or ""
            haystack = f"{event.company_name or ''} {desc}".lower()
            if not _industry_matches(haystack, industry_kw):
                filtered_out["industry"] += 1
                continue

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
                reason=f"CN IPO via akshare ({country} enabled)",
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
        "cn_ipo_sync_ok",
        fetched=len(result.items),
        events_created=events_created,
        matched=matched,
        notifications=notifications_created,
        filtered=filtered_out,
    )
    return CnIpoSyncSummary(
        fetched=len(result.items),
        events_created=events_created,
        matched=matched,
        notifications_created=notifications_created,
    )
