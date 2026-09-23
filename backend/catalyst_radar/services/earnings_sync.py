from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.adapters.eodhd import EodhdConfigError
from catalyst_radar.adapters.eodhd_calendar import (
    EodhdEarningsAdapter,
    default_window,
)
from catalyst_radar.config import settings
from catalyst_radar.dedup import dedup_key as make_dedup_key
from catalyst_radar.logging import get_logger
from catalyst_radar.models.base import today_local
from catalyst_radar.models.event import Event, EventRelevance
from catalyst_radar.models.notification import Notification
from catalyst_radar.repositories.company_repository import (
    TrackedCompanyRepository,
)
from catalyst_radar.repositories.event_repository import EventRepository
from catalyst_radar.repositories.notification_repository import (
    NotificationRepository,
)
from catalyst_radar.repositories.source_repository import (
    RawItemRepository,
    SourceRunRepository,
)
from catalyst_radar.services.alerts import format_earnings, format_earnings_results
from catalyst_radar.services.ipo_sync import _ipo_windows, _pick_window

log = get_logger(__name__)


@dataclass(slots=True)
class EarningsSyncSummary:
    fetched: int
    events_created: int
    matched: int
    notifications_created: int
    results_alerts: int = 0  # post-report re-alerts fired when `actual` lands


async def sync_earnings(
    session: AsyncSession,
    adapter: EodhdEarningsAdapter | None = None,
) -> EarningsSyncSummary:
    adapter = adapter or EodhdEarningsAdapter()
    runs = SourceRunRepository(session)
    run = await runs.start("eodhd.earnings")

    try:
        result = await adapter.fetch(default_window(settings.eodhd_earnings_lookahead_days))
    except EodhdConfigError as exc:
        await runs.finish(run, status="skipped", last_error=str(exc))
        log.warning("earnings_sync_skipped", error=str(exc))
        return EarningsSyncSummary(0, 0, 0, 0)
    except Exception as exc:  # noqa: BLE001
        await runs.finish(run, status="failed", last_error=repr(exc))
        log.warning("earnings_sync_failed", error=repr(exc))
        return EarningsSyncSummary(0, 0, 0, 0)

    await RawItemRepository(session).store(
        source_name=result.source_name,
        schema_name=result.schema_name,
        raw_payload=result.payload,
        source_url=result.source_url,
        http_status=result.http_status,
        source_run_id=run.id,
    )

    if result.http_status != 200:
        await runs.finish(run, status="failed", last_error=f"http {result.http_status}")
        await session.commit()
        return EarningsSyncSummary(0, 0, 0, 0)

    tracked = await TrackedCompanyRepository(session).list_active()
    tracked_index = {(t.exchange.upper(), t.symbol.upper()): t for t in tracked}

    from catalyst_radar.runtime_config import effective

    cfg = await effective(session)
    events_repo = EventRepository(session)
    notif_repo = NotificationRepository(session)
    windows = _ipo_windows(cfg.earnings_alert_windows_days)
    today = today_local(cfg.alert_timezone)

    events_created = 0
    matched = 0
    notifications_created = 0

    results_alerts = 0

    for raw in result.items:
        norm = adapter.normalize(raw)
        if not norm["symbol"]:
            continue
        sid = adapter.source_event_id(raw)
        # Capture the existing row's `actual` BEFORE upsert merges the new
        # payload over the top — that's how we detect a null→value flip
        # (EODHD populates `actual` shortly after the report) and fire the
        # post-results re-alert. Cheap extra query, no schema change needed.
        existing = await events_repo.get_by_source_event_id(sid)
        prior_actual = (existing.payload or {}).get("actual") if existing is not None else None

        event = Event(
            event_type="earnings",
            source_name=adapter.source_name,
            source_event_id=sid,
            dedup_key=adapter.dedup_key(raw),
            symbol=norm["symbol"],
            exchange=norm["exchange"],
            company_name=norm["company_name"],
            title=norm["title"],
            event_date=norm["event_date"],
            payload=norm["payload"],
        )
        event, created = await events_repo.upsert(event)
        if created:
            events_created += 1

        key = (
            (norm["exchange"] or "").upper(),
            (norm["symbol"] or "").upper(),
        )
        tc = tracked_index.get(key)
        if tc is None:
            continue

        matched += 1
        if event.company_name is None:
            event.company_name = tc.company_name
        session.add(
            EventRelevance(
                event_id=event.id,
                tracked_company_id=tc.id,
                matched=True,
                reason="direct tracked company earnings",
            )
        )

        if event.event_date is None:
            continue
        days_until = (event.event_date.date() - today).days
        window = _pick_window(days_until, windows)
        if window is not None:
            dkey = make_dedup_key(sid, "earnings", str(window))
            if await notif_repo.get_by_dedup_key(dkey) is None:
                session.add(
                    Notification(
                        event_id=event.id,
                        channel="telegram",
                        dedup_key=dkey,
                        reminder_window=str(window),
                        status="pending",
                        payload={"text": format_earnings(event)},
                    )
                )
                notifications_created += 1

        # Post-results re-alert: EODHD populates `actual` shortly after the
        # report. Fire a second notification (separate dedup_key suffixed
        # `results`) so the trader gets the beat/miss summary even when the
        # pre-event window has already passed (e.g. report shifted earlier
        # than the smallest configured window).
        new_actual = (event.payload or {}).get("actual")
        if prior_actual is None and new_actual is not None:
            results_dkey = make_dedup_key(sid, "earnings", "results")
            if await notif_repo.get_by_dedup_key(results_dkey) is None:
                history = await events_repo.recent_quarterly_actuals(
                    norm["symbol"], norm["exchange"], limit=4, exclude_event_id=event.id
                )
                session.add(
                    Notification(
                        event_id=event.id,
                        channel="telegram",
                        dedup_key=results_dkey,
                        reminder_window="results",
                        status="pending",
                        payload={"text": format_earnings_results(event, history=history)},
                    )
                )
                notifications_created += 1
                results_alerts += 1

    await session.commit()
    await runs.finish(run, status="success", item_count=len(result.items))
    await session.commit()
    log.info(
        "earnings_sync_ok",
        fetched=len(result.items),
        events_created=events_created,
        matched=matched,
        notifications=notifications_created,
        results_alerts=results_alerts,
    )
    return EarningsSyncSummary(
        fetched=len(result.items),
        events_created=events_created,
        matched=matched,
        notifications_created=notifications_created,
        results_alerts=results_alerts,
    )
