from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.models.base import utcnow
from catalyst_radar.models.event import Event, EventRelevance
from catalyst_radar.models.notification import TelegramChat
from catalyst_radar.services.digest import _render, run_digests
from tests.test_telegram import FakeTelegramClient


def _catalyst(**over) -> Event:
    base = dict(
        event_type="catalyst",
        source_name="test",
        source_event_id="r1",
        dedup_key="r1",
        symbol="MU",
        exchange="US",
        company_name="Micron Technology",
        title="Micron board appointment",
        event_date=utcnow(),
    )
    base.update(over)
    return Event(**base)


def test_render_puts_company_name_on_its_own_line() -> None:
    out = _render("Daily digest — today", [_catalyst()])
    lines = out.splitlines()
    idx = next(i for i, line in enumerate(lines) if "$MU" in line)
    # Ticker line carries the badge/ticker/date; the name lives on the NEXT line.
    assert "Micron Technology" not in lines[idx]
    assert "Micron Technology" in lines[idx + 1]


def test_render_omits_name_line_when_absent() -> None:
    out = _render("Daily digest — today", [_catalyst(company_name=None)])
    # No company_name → no second line, just the single ticker bullet.
    assert "$MU" in out
    assert sum(1 for line in out.splitlines() if line.strip().startswith("•")) == 1
    assert out.count("$MU") == 1


def test_render_escapes_company_name() -> None:
    out = _render("Daily digest — today", [_catalyst(company_name="Tom & Jerry <Co>")])
    assert "Tom &amp; Jerry &lt;Co&gt;" in out
    assert "<Co>" not in out


async def _seed(session: AsyncSession, *, sid: str, event_date) -> None:
    """One active chat + one watchlist-matched IPO event on `event_date`."""
    session.add(TelegramChat(chat_id="42", is_active=True))
    ev = Event(
        event_type="ipo",
        source_name="test",
        source_event_id=sid,
        dedup_key=sid,
        symbol="AAPL",
        exchange="US",
        company_name="Apple",
        title="Apple IPO",
        event_date=event_date,
    )
    session.add(ev)
    await session.flush()
    session.add(EventRelevance(event_id=ev.id, matched=True))
    await session.commit()


def _this_monday(hour: int):
    base = utcnow()
    return (base - timedelta(days=base.weekday())).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )


async def test_daily_digest_sends_and_is_idempotent(db_session: AsyncSession) -> None:
    # _events_between uses the real clock for its window, so seed "today".
    await _seed(db_session, sid="d1", event_date=utcnow())
    client = FakeTelegramClient()
    # Tuesday 08:00 (default daily_digest_hour=8) — a non-Monday so the weekly
    # branch never co-fires regardless of what real weekday it is today.
    gate = _this_monday(8) + timedelta(days=1)

    s1 = await run_digests(db_session, client=client, now=gate)
    assert s1.daily_sent == 1
    assert len(client.sent) == 1
    assert "Daily digest" in client.sent[0][1]

    # Same day, same hour → already sent, no resend.
    s2 = await run_digests(db_session, client=client, now=gate)
    assert s2.daily_sent == 0
    assert len(client.sent) == 1


async def test_digest_skips_off_hour(db_session: AsyncSession) -> None:
    await _seed(db_session, sid="o1", event_date=utcnow())
    client = FakeTelegramClient()
    gate = _this_monday(15)  # neither daily (8) nor weekly (Mon 8) matches

    s = await run_digests(db_session, client=client, now=gate)
    assert s.daily_sent == 0
    assert s.weekly_sent == 0
    assert client.sent == []


async def test_weekly_digest_sends_on_configured_day(db_session: AsyncSession) -> None:
    # Event 2 days out — in the weekly (next-7-days) window but not "today".
    await _seed(db_session, sid="w1", event_date=utcnow() + timedelta(days=2))
    client = FakeTelegramClient()
    gate = _this_monday(8)  # default weekly_digest_day=MONDAY, weekly_digest_hour=8

    s = await run_digests(db_session, client=client, now=gate)
    assert s.weekly_sent == 1
    assert any("Weekly digest" in t for _, t in client.sent)
