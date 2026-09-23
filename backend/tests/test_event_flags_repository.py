from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.models.event import Event
from catalyst_radar.repositories.event_flags_repository import EventFlagsRepository

_TZ = "Asia/Shanghai"


async def _ipo(session: AsyncSession, *, sid: str, event_date, symbol: str = "AAPL") -> Event:
    ev = Event(
        event_type="ipo",
        source_name="t",
        source_event_id=sid,
        dedup_key=sid,
        symbol=symbol,
        exchange="US",
        company_name="Apple",
        title="IPO",
        event_date=event_date,
    )
    session.add(ev)
    await session.flush()
    return ev


async def test_set_starred_upserts_and_stamps(db_session: AsyncSession) -> None:
    ev = await _ipo(db_session, sid="s1", event_date=datetime.now(UTC))
    repo = EventFlagsRepository(db_session)

    row = await repo.set_starred(ev.id, "c1", True)
    assert row.starred is True and row.starred_at is not None

    row2 = await repo.set_starred(ev.id, "c1", False)
    assert row2.starred is False and row2.starred_at is None


async def test_dismissed_ids(db_session: AsyncSession) -> None:
    a = await _ipo(db_session, sid="a", event_date=datetime.now(UTC))
    await _ipo(db_session, sid="b", event_date=datetime.now(UTC), symbol="MSFT")
    repo = EventFlagsRepository(db_session)
    await repo.set_dismissed(a.id, "c1", True)
    assert await repo.dismissed_ids("c1") == {a.id}


async def test_starred_ipo_events_only_starred(db_session: AsyncSession) -> None:
    ev = await _ipo(db_session, sid="x", event_date=datetime.now(UTC))
    repo = EventFlagsRepository(db_session)
    assert await repo.starred_ipo_events("c1") == []
    await repo.set_starred(ev.id, "c1", True)
    assert [e.id for e in await repo.starred_ipo_events("c1")] == [ev.id]


async def test_due_day_of_local_date_and_marker(db_session: AsyncSession) -> None:
    today = datetime.now(ZoneInfo(_TZ)).date()
    list_dt = datetime(today.year, today.month, today.day, 1, 0, tzinfo=ZoneInfo(_TZ))
    ev = await _ipo(db_session, sid="d1", event_date=list_dt)
    repo = EventFlagsRepository(db_session)

    assert await repo.due_day_of(today, _TZ) == []  # not starred yet
    await repo.set_starred(ev.id, "c1", True)
    assert await repo.due_day_of(today, _TZ) == [("c1", ev)]

    await repo.mark_day_of_notified(ev.id, "c1")
    assert await repo.due_day_of(today, _TZ) == []  # marker suppresses re-fire


async def test_due_day_of_excludes_other_dates(db_session: AsyncSession) -> None:
    today = datetime.now(ZoneInfo(_TZ)).date()
    tomorrow = datetime.now(ZoneInfo(_TZ)) + timedelta(days=1)
    ev = await _ipo(db_session, sid="t1", event_date=tomorrow)
    repo = EventFlagsRepository(db_session)
    await repo.set_starred(ev.id, "c1", True)
    assert await repo.due_day_of(today, _TZ) == []


async def test_due_day_of_star_wins_over_dismiss(db_session: AsyncSession) -> None:
    today = datetime.now(ZoneInfo(_TZ)).date()
    list_dt = datetime(today.year, today.month, today.day, 1, 0, tzinfo=ZoneInfo(_TZ))
    ev = await _ipo(db_session, sid="sd1", event_date=list_dt)
    repo = EventFlagsRepository(db_session)
    await repo.set_starred(ev.id, "c1", True)
    await repo.set_dismissed(ev.id, "c1", True)
    assert await repo.due_day_of(today, _TZ) == [("c1", ev)]


async def test_flags_are_independent_per_chat(db_session: AsyncSession) -> None:
    today = datetime.now(ZoneInfo(_TZ)).date()
    list_dt = datetime(today.year, today.month, today.day, 1, 0, tzinfo=ZoneInfo(_TZ))
    ev = await _ipo(db_session, sid="pc1", event_date=list_dt)
    repo = EventFlagsRepository(db_session)

    await repo.set_starred(ev.id, "alice", True)
    await repo.set_dismissed(ev.id, "bob", True)

    assert [e.id for e in await repo.starred_ipo_events("alice")] == [ev.id]
    assert await repo.starred_ipo_events("bob") == []
    assert await repo.dismissed_ids("alice") == set()
    assert await repo.dismissed_ids("bob") == {ev.id}
    assert await repo.starred_chat_ids(ev.id) == {"alice"}
    assert await repo.due_day_of(today, _TZ) == [("alice", ev)]

    # Marking alice's reminder leaves a later star by bob still due.
    await repo.mark_day_of_notified(ev.id, "alice")
    await repo.set_starred(ev.id, "bob", True)
    assert await repo.due_day_of(today, _TZ) == [("bob", ev)]
