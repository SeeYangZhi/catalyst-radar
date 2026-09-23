"""Retention sweeps for append-only audit tables.

``source_runs`` (and the ``raw_items`` that reference it) grow ~200 rows/day
forever — one row per scheduled source fetch. Nothing reads beyond the last
day or two (the self-monitor windows are <= 24h; the Source Runs page shows a
handful of recent rows), so a daily prune keeps the tables bounded without
losing anything useful. Runs as its own beat task; safe to run repeatedly.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.config import settings
from catalyst_radar.logging import get_logger
from catalyst_radar.models.base import utcnow
from catalyst_radar.repositories.source_repository import (
    RawItemRepository,
    SourceRunRepository,
)

log = get_logger(__name__)

# Never prune below this many days, whatever the config says — the watchdog
# reasons over the last ~24h and must keep history to tell stalled from idle.
_MIN_RETENTION_DAYS = 2


@dataclass(slots=True)
class PruneResult:
    raw_items: int
    source_runs: int
    retention_days: int


async def prune_source_runs(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    retention_days: int | None = None,
) -> PruneResult:
    """Delete source_runs (and their raw_items) older than the retention
    window. raw_items go first because they FK source_runs. ``now`` and
    ``retention_days`` are injectable for tests."""
    days = (
        retention_days
        if retention_days is not None
        else int(settings.source_runs_retention_days)
    )
    days = max(_MIN_RETENTION_DAYS, days)
    cutoff = (now or utcnow()) - timedelta(days=days)

    raw = await RawItemRepository(session).delete_for_runs_started_before(cutoff)
    runs = await SourceRunRepository(session).delete_started_before(cutoff)
    await session.commit()

    log.info(
        "prune_source_runs", retention_days=days, raw_items=raw, source_runs=runs
    )
    return PruneResult(raw_items=raw, source_runs=runs, retention_days=days)
