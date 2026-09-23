"""source_runs / raw_items retention sweep."""

from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.models.base import utcnow
from catalyst_radar.models.source import RawItem, SourceRun
from catalyst_radar.repositories.source_repository import SourceRunRepository
from catalyst_radar.services.retention import prune_source_runs


async def _run_at(session: AsyncSession, *, started_at) -> SourceRun:
    run = SourceRun(
        source_name="x", status="success", started_at=started_at, finished_at=started_at
    )
    session.add(run)
    await session.flush()  # populate id for the raw_item FK
    return run


async def test_prune_deletes_old_runs_and_their_raw_items(db_session: AsyncSession) -> None:
    now = utcnow()
    old = await _run_at(db_session, started_at=now - timedelta(days=40))
    new = await _run_at(db_session, started_at=now - timedelta(days=2))
    db_session.add(
        RawItem(source_name="x", schema_name="x.v1", raw_payload={}, source_run_id=old.id)
    )
    db_session.add(
        RawItem(source_name="x", schema_name="x.v1", raw_payload={}, source_run_id=new.id)
    )
    await db_session.commit()

    res = await prune_source_runs(db_session, now=now, retention_days=30)
    assert res.source_runs == 1  # only the 40-day-old run
    assert res.raw_items == 1  # its raw_item went first (FK)

    remaining = await SourceRunRepository(db_session).list_recent()
    assert [r.id for r in remaining] == [new.id]


async def test_prune_clamps_minimum_retention(db_session: AsyncSession) -> None:
    """retention_days below the floor must be clamped so the watchdog never
    loses its recent history."""
    now = utcnow()
    await _run_at(db_session, started_at=now - timedelta(days=1))
    await db_session.commit()

    res = await prune_source_runs(db_session, now=now, retention_days=0)
    assert res.retention_days == 2  # clamped up from 0
    assert res.source_runs == 0  # the 1-day-old run survives

    remaining = await SourceRunRepository(db_session).list_recent()
    assert len(remaining) == 1
