"""Mainland China share-unlock catalyst sync.

For every active CN-tracked company (SSE + SZSE), pull tushare's
`share_float` schedule, aggregate by `float_date`, and create one
catalyst Event per upcoming-unlock date in the look-ahead window. The
event uses ``event_subtype='lockup_unlock'`` so the rest of the
classifier/notification path treats it like any other catalyst.

Notification windows (T-30/T-14/T-7/T-3/T-1) reuse the same per-window
dedup pattern as the IPO syncs — one alert per (event, window) pair,
idempotent across re-runs. Importance is derived from `float_ratio`
via the runtime-configurable thresholds.

Per-ticker because tushare's free-tier `share_float` doesn't support a
bulk "all upcoming unlocks" query. We iterate tracked companies, with
the per-ticker fetch offloaded to a thread by the adapter (tushare is
blocking).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.adapters.tushare_unlocks import (
    TushareUnlocksAdapter,
    UpcomingUnlock,
    group_upcoming,
    importance_for_ratio,
    utcnow_date,
)
from catalyst_radar.dedup import dedup_key as make_dedup_key
from catalyst_radar.dedup import source_event_id as make_source_event_id
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
from catalyst_radar.repositories.source_repository import SourceRunRepository
from catalyst_radar.services.alerts import format_unlock
from catalyst_radar.services.ipo_sync import _ipo_windows, _pick_window

log = get_logger(__name__)


@dataclass(slots=True)
class CnUnlocksSyncSummary:
    companies_checked: int
    upcoming_total: int
    events_created: int
    notifications_created: int
    errors: int


def _source_event_id(symbol: str, exchange: str, float_date_iso: str) -> str:
    """Stable per (ticker, unlock-date). The same date may carry multiple
    holders (we aggregate); the date is the identity, not the holder."""
    return make_source_event_id(
        exchange.lower(), "unlock", symbol, float_date_iso
    )


def _build_payload(u: UpcomingUnlock, importance: str) -> dict[str, Any]:
    return {
        "source": "tushare_share_float",
        "unlock": {
            "ts_code": u.ts_code,
            "float_date": u.float_date.isoformat(),
            "total_share": u.total_share,
            "total_ratio": u.total_ratio,
            "share_type": u.primary_share_type,
            "holders": [
                {
                    "name": h.holder_name,
                    "share": h.float_share,
                    "ratio": h.float_ratio,
                    "share_type": h.share_type,
                    "ann_date": h.ann_date.isoformat() if h.ann_date else None,
                }
                for h in u.holders
            ],
        },
        # Synthesize a classification block so existing renderers /
        # review-queue filters work without special-casing this source.
        "classification": {
            "is_company_critical": True,
            "event_subtype": "lockup_unlock",
            "importance": importance,
            "confidence": 1.0,
            "summary": (
                f"{u.symbol}.{u.exchange} unlock {u.total_ratio:.2f}% "
                f"on {u.float_date.isoformat()}"
            ),
            "expected_impact": "Supply overhang from unlocked shares hitting the float.",
            "why_it_matters": "Lockup expiries often pressure price near the unlock date.",
            "suggested_action": "watch" if importance != "high" else "research",
            "ignore_reason": "",
        },
    }


async def sync_cn_unlocks(
    session: AsyncSession,
    adapter: TushareUnlocksAdapter | None = None,
) -> CnUnlocksSyncSummary:
    from catalyst_radar.config import settings
    from catalyst_radar.runtime_config import effective

    eff = await effective(session)
    if not bool(eff.cn_unlocks_enabled):
        log.info("cn_unlocks_sync_disabled")
        return CnUnlocksSyncSummary(0, 0, 0, 0, 0)

    adapter = adapter or TushareUnlocksAdapter()
    if not adapter.configured:
        log.info("cn_unlocks_sync_skipped", reason="no_tushare_token")
        return CnUnlocksSyncSummary(0, 0, 0, 0, 0)

    tracked = await TrackedCompanyRepository(session).list_active()
    cn_companies = [tc for tc in tracked if (tc.exchange or "").upper() in {"SSE", "SZSE"}]
    if not cn_companies:
        log.info("cn_unlocks_sync_no_targets")
        return CnUnlocksSyncSummary(0, 0, 0, 0, 0)

    runs = SourceRunRepository(session)
    run = await runs.start("tushare.share_float")
    events_repo = EventRepository(session)
    notif_repo = NotificationRepository(session)

    windows = _ipo_windows(eff.unlock_alert_windows_days)
    today_dt = today_local(eff.alert_timezone)
    today = utcnow_date()
    lookahead = int(settings.cn_unlocks_lookahead_days)
    high_thr = float(settings.cn_unlocks_high_ratio_pct)
    med_thr = float(settings.cn_unlocks_medium_ratio_pct)

    upcoming_total = events_created = notifications_created = errors = 0

    for tc in cn_companies:
        try:
            rows = await adapter.fetch_unlocks(symbol=tc.symbol, exchange=tc.exchange)
        except Exception as exc:  # noqa: BLE001 - one ticker must not abort the run
            errors += 1
            log.warning("cn_unlocks_fetch_failed", symbol=tc.symbol, error=repr(exc))
            continue

        upcoming = group_upcoming(
            rows, symbol=tc.symbol, exchange=tc.exchange, today=today, lookahead_days=lookahead
        )
        upcoming_total += len(upcoming)

        for u in upcoming:
            sid = _source_event_id(tc.symbol, tc.exchange, u.float_date.isoformat())
            importance = importance_for_ratio(
                u.total_ratio, high_threshold=high_thr, medium_threshold=med_thr
            )
            payload = _build_payload(u, importance)
            event = Event(
                event_type="catalyst",
                source_name="tushare_share_float",
                source_event_id=sid,
                dedup_key=make_dedup_key(sid),
                symbol=tc.symbol,
                exchange=tc.exchange,
                country="CN",
                company_name=tc.company_name,
                title=(
                    f"{tc.symbol} unlock {u.total_ratio:.2f}% on {u.float_date.isoformat()}"
                ),
                event_date=datetime.combine(u.float_date, datetime.min.time(), tzinfo=UTC),
                payload=payload,
                status="notified",
            )
            event, created = await events_repo.upsert(event)
            if created:
                events_created += 1

            session.add(
                EventRelevance(
                    event_id=event.id,
                    tracked_company_id=tc.id,
                    matched=True,
                    reason="CN share unlock (tushare share_float)",
                )
            )

            days_until = (u.float_date - today_dt).days
            window = _pick_window(days_until, windows)
            if window is None:
                continue
            dkey = make_dedup_key(sid, "unlock", str(window))
            if await notif_repo.get_by_dedup_key(dkey) is not None:
                continue
            session.add(
                Notification(
                    event_id=event.id,
                    channel="telegram",
                    dedup_key=dkey,
                    reminder_window=str(window),
                    status="pending",
                    payload={"text": format_unlock(event)},
                )
            )
            notifications_created += 1

    await session.commit()
    await runs.finish(
        run, status="success", item_count=upcoming_total
    )
    await session.commit()
    log.info(
        "cn_unlocks_sync_ok",
        companies=len(cn_companies),
        upcoming=upcoming_total,
        events_created=events_created,
        notifications=notifications_created,
        errors=errors,
    )
    return CnUnlocksSyncSummary(
        companies_checked=len(cn_companies),
        upcoming_total=upcoming_total,
        events_created=events_created,
        notifications_created=notifications_created,
        errors=errors,
    )
