from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.models.event import Event
from catalyst_radar.models.notification import TelegramChat
from catalyst_radar.repositories.event_flags_repository import EventFlagsRepository
from catalyst_radar.runtime_config import effective
from catalyst_radar.services.ipo_reminders import run_day_of_reminders
from tests.test_telegram import FakeTelegramClient


async def _setup(db_session: AsyncSession, *, sid: str):
    """One active chat + one IPO listing 'today' in the configured tz.
    Returns (event, tz_name, digest_hour)."""
    cfg = await effective(db_session)
    tz = cfg.alert_timezone
    hour = int(cfg.daily_digest_hour)
    today = datetime.now(ZoneInfo(tz)).date()
    list_dt = datetime(today.year, today.month, today.day, 1, 0, tzinfo=ZoneInfo(tz))
    db_session.add(TelegramChat(chat_id="42", is_active=True))
    ev = Event(
        event_type="ipo",
        source_name="t",
        source_event_id=sid,
        dedup_key=sid,
        symbol="AAA",
        exchange="US",
        company_name="Alpha",
        title="IPO: Alpha",
        event_date=list_dt,
        payload={"profile": {"description": "Alpha makes widgets."}},
    )
    db_session.add(ev)
    await db_session.flush()
    return ev, tz, hour


def _at(tz: str, hour: int) -> datetime:
    today = datetime.now(ZoneInfo(tz)).date()
    return datetime(today.year, today.month, today.day, hour, 0, tzinfo=ZoneInfo(tz))


async def test_reminds_starred_once(db_session: AsyncSession) -> None:
    ev, tz, hour = await _setup(db_session, sid="r1")
    await EventFlagsRepository(db_session).set_starred(ev.id, "42", True)
    client = FakeTelegramClient()
    now = _at(tz, hour)

    s1 = await run_day_of_reminders(db_session, client=client, now=now)
    assert s1.reminded == 1
    assert "Listing today" in client.sent[0][1]

    s2 = await run_day_of_reminders(db_session, client=client, now=now)
    assert s2.reminded == 0
    assert len(client.sent) == 1


async def _seed_starred_ipo(
    db_session: AsyncSession, *, sid: str, tz: str, chat_ids: tuple[str, ...]
) -> Event:
    today = datetime.now(ZoneInfo(tz)).date()
    list_dt = datetime(today.year, today.month, today.day, 1, 0, tzinfo=ZoneInfo(tz))
    ev = Event(
        event_type="ipo",
        source_name="t",
        source_event_id=sid,
        dedup_key=sid,
        symbol="AAA",
        exchange="US",
        company_name="Alpha",
        title="IPO: Alpha",
        event_date=list_dt,
    )
    db_session.add(ev)
    await db_session.flush()
    for cid in chat_ids:
        await EventFlagsRepository(db_session).set_starred(ev.id, cid, True)
    return ev


async def test_chat_username_is_tagged(db_session: AsyncSession) -> None:
    cfg = await effective(db_session)
    tz, hour = cfg.alert_timezone, int(cfg.daily_digest_hour)
    db_session.add(
        TelegramChat(
            chat_id="100000001",
            chat_type="private",
            username="alice_example",
            is_active=True,
        )
    )
    await _seed_starred_ipo(db_session, sid="u1", tz=tz, chat_ids=("100000001",))
    client = FakeTelegramClient()

    s = await run_day_of_reminders(db_session, client=client, now=_at(tz, hour))
    assert s.reminded == 1
    # Literal @handle tag on the banner line — Telegram auto-links it.
    assert client.sent[0][1].startswith("⭐ <b>Listing today</b> @alice_example\n")


async def test_no_tag_when_chat_has_no_username(db_session: AsyncSession) -> None:
    cfg = await effective(db_session)
    tz, hour = cfg.alert_timezone, int(cfg.daily_digest_hour)
    db_session.add(TelegramChat(chat_id="42", chat_type="private", is_active=True))
    await _seed_starred_ipo(db_session, sid="u2", tz=tz, chat_ids=("42",))
    client = FakeTelegramClient()

    await run_day_of_reminders(db_session, client=client, now=_at(tz, hour))
    assert client.sent[0][1].split("\n", 1)[0] == "⭐ <b>Listing today</b>"


async def test_each_chat_tagged_with_own_username(db_session: AsyncSession) -> None:
    cfg = await effective(db_session)
    tz, hour = cfg.alert_timezone, int(cfg.daily_digest_hour)
    db_session.add(TelegramChat(chat_id="111", username="alice", is_active=True))
    db_session.add(TelegramChat(chat_id="222", username="bob", is_active=True))
    await _seed_starred_ipo(db_session, sid="u3", tz=tz, chat_ids=("111", "222"))
    client = FakeTelegramClient()

    await run_day_of_reminders(db_session, client=client, now=_at(tz, hour))
    sent = dict(client.sent)  # chat_id -> text; each tagged with its own user
    assert "@alice" in sent["111"] and "@bob" not in sent["111"]
    assert "@bob" in sent["222"] and "@alice" not in sent["222"]


async def test_skips_before_digest_hour(db_session: AsyncSession) -> None:
    ev, tz, hour = await _setup(db_session, sid="r2")
    await EventFlagsRepository(db_session).set_starred(ev.id, "42", True)
    client = FakeTelegramClient()
    before = _at(tz, hour) - timedelta(hours=1)
    s = await run_day_of_reminders(db_session, client=client, now=before)
    assert s.reminded == 0
    assert client.sent == []


async def test_ignores_unstarred(db_session: AsyncSession) -> None:
    ev, tz, hour = await _setup(db_session, sid="r3")
    client = FakeTelegramClient()
    s = await run_day_of_reminders(db_session, client=client, now=_at(tz, hour))
    assert s.reminded == 0


async def test_star_wins_over_dismiss(db_session: AsyncSession) -> None:
    ev, tz, hour = await _setup(db_session, sid="r4")
    repo = EventFlagsRepository(db_session)
    await repo.set_starred(ev.id, "42", True)
    await repo.set_dismissed(ev.id, "42", True)
    client = FakeTelegramClient()
    s = await run_day_of_reminders(db_session, client=client, now=_at(tz, hour))
    assert s.reminded == 1


async def test_marks_even_when_all_sends_fail(db_session: AsyncSession) -> None:
    ev, tz, hour = await _setup(db_session, sid="r5")
    await EventFlagsRepository(db_session).set_starred(ev.id, "42", True)
    failing = FakeTelegramClient(ok=False)
    s = await run_day_of_reminders(db_session, client=failing, now=_at(tz, hour))
    assert s.reminded == 0  # no chat succeeded
    flags = await EventFlagsRepository(db_session).get(ev.id, "42")
    assert flags.day_of_notified_at is not None  # marker still set → no re-send next tick

    # A second tick finds nothing due (marker suppresses it).
    ok = FakeTelegramClient(ok=True)
    s2 = await run_day_of_reminders(db_session, client=ok, now=_at(tz, hour))
    assert s2.reminded == 0
    assert ok.sent == []


async def test_only_chats_that_starred_are_reminded(db_session: AsyncSession) -> None:
    cfg = await effective(db_session)
    tz, hour = cfg.alert_timezone, int(cfg.daily_digest_hour)
    db_session.add(TelegramChat(chat_id="111", is_active=True))
    db_session.add(TelegramChat(chat_id="222", is_active=True))
    await _seed_starred_ipo(db_session, sid="u4", tz=tz, chat_ids=("111",))
    client = FakeTelegramClient()

    s = await run_day_of_reminders(db_session, client=client, now=_at(tz, hour))
    assert s.reminded == 1
    assert [c for c, _ in client.sent] == ["111"]
