"""CN IPO review-stage discovery (上会 / approval / rejection).

Pulls CSRC review-committee hearing rows via
``AkshareIpoReviewAdapter`` and creates an event per
(reservation_code, status) pair. A status transition for the
same company (e.g. 未上会 → 上会通过) creates a new event
and a fresh alert because the adapter encodes status in the
``source_event_id``.

Notification dispatch is a one-shot — review-stage events
don't get reminder windows; the alert fires once when the
status row first appears. The alert window is bounded so the
first sync against historical data doesn't flood the channel
(``[today - 14d, today + 90d]``).

All three mainland boards are alerted — SSE, SZSE, and BSE.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.adapters.akshare_ipo_review import (
    AkshareIpoReviewAdapter,
    eastmoney_search_url,
)
from catalyst_radar.dedup import dedup_key as make_dedup_key
from catalyst_radar.logging import get_logger
from catalyst_radar.models.base import today_local
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

log = get_logger(__name__)


# Drop alerts whose meeting date sits outside this window. Old rows
# (months-stale approvals) are still inserted as events for ingest, but
# never alerted — the user can still see them in the IPO Filters page.
_ALERT_PAST_DAYS = 14
_ALERT_FUTURE_DAYS = 90


@dataclass(slots=True)
class CnIpoReviewSyncSummary:
    fetched: int
    events_created: int
    matched: int
    notifications_created: int


async def sync_cn_ipo_review(
    session: AsyncSession,
    adapter: AkshareIpoReviewAdapter | None = None,
) -> CnIpoReviewSyncSummary:
    from catalyst_radar.runtime_config import effective

    eff = await effective(session)
    if not bool(eff.cn_ipo_review_enabled):
        log.info("cn_ipo_review_sync_disabled")
        return CnIpoReviewSyncSummary(0, 0, 0, 0)

    adapter = adapter or AkshareIpoReviewAdapter()
    runs = SourceRunRepository(session)
    run = await runs.start("akshare.stock_ipo_review_em")

    try:
        result = await adapter.fetch()
    except Exception as exc:  # noqa: BLE001
        await runs.finish(run, status="failed", last_error=repr(exc))
        log.warning("cn_ipo_review_sync_failed", error=repr(exc))
        return CnIpoReviewSyncSummary(0, 0, 0, 0)

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
        return CnIpoReviewSyncSummary(0, 0, 0, 0)

    events_repo = EventRepository(session)
    notif_repo = NotificationRepository(session)
    today = today_local(eff.alert_timezone)
    earliest_alert = today - timedelta(days=_ALERT_PAST_DAYS)
    latest_alert = today + timedelta(days=_ALERT_FUTURE_DAYS)

    events_created = matched = notifications_created = 0
    skipped = {"out_of_window": 0, "duplicate_alert": 0}

    for raw in result.items:
        norm = adapter.normalize(raw)
        sid = adapter.source_event_id(raw)

        event = Event(
            event_type="ipo",
            source_name=adapter.source_name,
            source_event_id=sid,
            dedup_key=adapter.dedup_key(raw),
            symbol=norm["symbol"],
            exchange=norm["exchange"],
            country="CN" if norm.get("exchange") in {"SSE", "SZSE", "BSE"} else None,
            company_name=norm["company_name"],
            title=norm["title"],
            event_date=norm["event_date"],
            # Per-company Eastmoney search link, not the shared committee
            # calendar (adapter.source_url) — the raw feed has no per-event
            # URL, so a name search is the most useful link we can build.
            source_url=eastmoney_search_url(norm["company_name"]),
            payload=norm["payload"],
        )
        event, created = await events_repo.upsert(event)
        if created:
            events_created += 1

        if not created:
            # Already-known (code, status) — never re-alerts. A genuine
            # status transition would have a different source_event_id
            # and land in the `created` branch above.
            continue

        meeting = event.event_date.date() if event.event_date else None
        if meeting is None or meeting < earliest_alert or meeting > latest_alert:
            skipped["out_of_window"] += 1
            continue

        matched += 1
        session.add(
            EventRelevance(
                event_id=event.id,
                matched=True,
                reason=f"CN IPO review ({raw['status']})",
            )
        )

        # One alert per (code, status) — `sid` already encodes status,
        # so the notification dedup key is just the sid.
        dkey = make_dedup_key(sid, "review")
        if await notif_repo.get_by_dedup_key(dkey) is not None:
            skipped["duplicate_alert"] += 1
            continue
        session.add(
            Notification(
                event_id=event.id,
                channel="telegram",
                dedup_key=dkey,
                reminder_window="review",
                status="pending",
                payload={"text": format_ipo(event)},
            )
        )
        notifications_created += 1

    await session.commit()
    await runs.finish(run, status="success", item_count=len(result.items))
    await session.commit()
    log.info(
        "cn_ipo_review_sync_ok",
        fetched=len(result.items),
        events_created=events_created,
        matched=matched,
        notifications=notifications_created,
        skipped=skipped,
    )
    return CnIpoReviewSyncSummary(
        fetched=len(result.items),
        events_created=events_created,
        matched=matched,
        notifications_created=notifications_created,
    )
