from datetime import datetime
from typing import Any

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.models.base import utcnow
from catalyst_radar.models.source import RawItem, SourceRun


class SourceRunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def start(self, source_name: str) -> SourceRun:
        run = SourceRun(source_name=source_name, status="running")
        self.session.add(run)
        await self.session.commit()
        await self.session.refresh(run)
        return run

    async def finish(
        self,
        run: SourceRun,
        *,
        status: str,
        item_count: int = 0,
        error_count: int = 0,
        last_error: str | None = None,
        summary: dict[str, Any] | None = None,
    ) -> SourceRun:
        run.status = status
        run.item_count = item_count
        run.error_count = error_count
        if last_error:
            run.last_error = last_error
        if summary is not None:
            run.summary = summary
        run.finished_at = utcnow()
        self.session.add(run)
        await self.session.commit()
        await self.session.refresh(run)
        return run

    async def latest(self, source_name: str) -> SourceRun | None:
        result = await self.session.execute(
            select(SourceRun)
            .where(SourceRun.source_name == source_name)
            .order_by(SourceRun.started_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def list_recent(self, limit: int = 50) -> list[SourceRun]:
        result = await self.session.execute(
            select(SourceRun).order_by(SourceRun.started_at.desc()).limit(limit)
        )
        return list(result.scalars().all())

    async def latest_success(self) -> SourceRun | None:
        """Most recently *finished* successful run across all sources. Used by
        the self-monitor to detect a stalled ingestion pipeline.

        Excludes rows with a NULL ``finished_at``: Postgres sorts NULLs FIRST
        under ``ORDER BY ... DESC``, so a success row missing its finish
        timestamp would otherwise masquerade as the latest and read back as
        None — a false "never succeeded" alarm."""
        result = await self.session.execute(
            select(SourceRun)
            .where(SourceRun.status == "success", SourceRun.finished_at.is_not(None))
            .order_by(SourceRun.finished_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def has_any_success(self) -> bool:
        """True if any source run has ever succeeded. Lets the self-monitor
        tell a genuine cold start (alert "never succeeded") from a transient
        empty ``latest_success()`` read on a system that has history."""
        result = await self.session.execute(
            select(SourceRun.id).where(SourceRun.status == "success").limit(1)
        )
        return result.first() is not None

    async def success_sources_since(self, since: datetime) -> set[str]:
        """Names of sources with at least one SUCCESS finished at/after
        ``since``. Lets the self-monitor judge per-source staleness against a
        window wider than the global gap, so a slow source that succeeds
        roughly every cycle isn't flagged for one transient blip."""
        result = await self.session.execute(
            select(SourceRun.source_name)
            .where(SourceRun.status == "success", SourceRun.finished_at >= since)
            .distinct()
        )
        return {name for (name,) in result.all()}

    async def runs_since(self, since: datetime) -> list[SourceRun]:
        """All runs started at/after ``since``, newest first. Lets the
        self-monitor reason per-source (latest run, success-in-window) rather
        than collapsing every source into one global signal."""
        result = await self.session.execute(
            select(SourceRun)
            .where(SourceRun.started_at >= since)
            .order_by(SourceRun.started_at.desc())
        )
        return list(result.scalars().all())

    async def failed_counts_since(self, since: datetime) -> dict[str, int]:
        """Number of FAILED runs per source started at/after ``since``. Drives
        the self-monitor's failure-streak (instability) tier — a source that
        keeps failing but recovers before the stale threshold. The streak
        window is typically wider than the stale-gap window, so this is its
        own focused query rather than a re-slice of ``runs_since``."""
        result = await self.session.execute(
            select(SourceRun.source_name, func.count())
            .where(SourceRun.started_at >= since, SourceRun.status == "failed")
            .group_by(SourceRun.source_name)
        )
        return {name: int(count) for name, count in result.all()}

    async def delete_started_before(self, cutoff: datetime) -> int:
        """Retention sweep: drop runs started before ``cutoff``. Returns the
        count deleted. Any raw_items referencing these runs must be deleted
        first (FK) — see RawItemRepository.delete_for_runs_started_before."""
        result = await self.session.execute(
            delete(SourceRun).where(SourceRun.started_at < cutoff)
        )
        return int(result.rowcount or 0)


class RawItemRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def store(
        self,
        *,
        source_name: str,
        schema_name: str,
        raw_payload: Any,
        source_url: str | None = None,
        http_status: int | None = None,
        source_event_id: str | None = None,
        dedup_key: str | None = None,
        source_run_id: int | None = None,
    ) -> RawItem:
        item = RawItem(
            source_name=source_name,
            schema_name=schema_name,
            raw_payload=raw_payload,
            source_url=source_url,
            http_status=http_status,
            source_event_id=source_event_id,
            dedup_key=dedup_key,
            source_run_id=source_run_id,
        )
        self.session.add(item)
        await self.session.flush()
        return item

    async def delete_for_runs_started_before(self, cutoff: datetime) -> int:
        """Retention sweep companion: drop raw_items belonging to source_runs
        started before ``cutoff`` (they FK those runs, so they must go first),
        plus any untied legacy raw_items older than ``cutoff``. Returns the
        count deleted."""
        result = await self.session.execute(
            delete(RawItem).where(
                or_(
                    RawItem.source_run_id.in_(
                        select(SourceRun.id).where(SourceRun.started_at < cutoff)
                    ),
                    and_(
                        RawItem.source_run_id.is_(None),
                        RawItem.created_at < cutoff,
                    ),
                )
            )
        )
        return int(result.rowcount or 0)
