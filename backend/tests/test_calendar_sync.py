import json
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.adapters.base import FetchResult
from catalyst_radar.adapters.eodhd_calendar import (
    EodhdEarningsAdapter,
    EodhdIpoAdapter,
)
from catalyst_radar.config import settings
from catalyst_radar.models.base import today_local
from catalyst_radar.models.company import TrackedCompany
from catalyst_radar.models.event import Event
from catalyst_radar.models.notification import Notification
from catalyst_radar.services.earnings_sync import sync_earnings
from catalyst_radar.services.ipo_sync import (
    _etf_trust_indicators,
    country_for_exchange,
    sync_ipos,
)

FX = Path(__file__).parent / "fixtures"

# Fixtures carry calendar dates that must stay ahead of "today" or the
# reminder-window logic skips them. Anchor every date to today (in the
# alert timezone, which is what the sync services compare against) so the
# suite can't age out — a fixed date silently broke this once already.
TODAY = today_local(settings.alert_timezone)


def _rel_date(days: int) -> str:
    return (TODAY + timedelta(days=days)).isoformat()


EARNINGS = json.loads((FX / "eodhd_earnings.json").read_text())
# windows 7,1,0 — AAPL at +1d must land in a window; others stay future.
for _rec, _offset in zip(EARNINGS["earnings"], (1, 4, 20), strict=True):
    _rec["report_date"] = _rel_date(_offset)

IPOS = json.loads((FX / "eodhd_ipos.json").read_text())
# windows 14,7,1 — US at +1d, HK at +7d both alert; Korea is country-excluded.
for _rec, _offset in zip(IPOS["ipos"], (1, 7, 11), strict=True):
    _rec["start_date"] = _rel_date(_offset)


class _EarningsStub(EodhdEarningsAdapter):
    def __init__(self) -> None:
        super().__init__(api_key="stub")

    async def fetch(self, target: tuple[date, date]) -> FetchResult:
        return FetchResult(
            source_name="eodhd",
            schema_name="eodhd.earnings.v1",
            source_url="https://eodhd.test/earnings",
            http_status=200,
            payload=EARNINGS,
            items=EARNINGS["earnings"],
        )


class _IpoStub(EodhdIpoAdapter):
    def __init__(self) -> None:
        super().__init__(api_key="stub")

    async def fetch(self, target: tuple[date, date]) -> FetchResult:
        return FetchResult(
            source_name="eodhd",
            schema_name="eodhd.ipos.v1",
            source_url="https://eodhd.test/ipos",
            http_status=200,
            payload=IPOS,
            items=IPOS["ipos"],
        )


def test_earnings_normalize_and_stable_id() -> None:
    a = EodhdEarningsAdapter(api_key="x")
    rec = EARNINGS["earnings"][0]
    norm = a.normalize(rec)
    assert norm["symbol"] == "AAPL"
    assert norm["exchange"] == "US"
    assert norm["payload"]["estimate"] == 1.23
    sid = a.source_event_id(rec)
    assert sid == f"eodhd:earnings:AAPL.US:{rec['report_date']}:2026-03-31"
    assert a.source_event_id(rec) == sid


def test_excluded_instruments() -> None:
    # SPACs, derivative classes, and fund/trust wrappers — drop.
    drop = [
        "FortuneX Acquisition Corporation Units",
        "Apogee Acquisition Corp Warrant",
        "Apogee Acquisition Corp Rights",
        "Innovative Digital Investors Acquisition Corp. Unit",
        "Direxion Shares ETF Trust",
        "VelocityShares 3x Long Silver ETN",
        "Elevation Series Trust",
        "FundVantage Trust",
        "Octave Intelligence plc Class B Ordinary Shares When Issued",
        "iShares Flexible Equity Active ETF",
    ]
    for name in drop:
        assert _etf_trust_indicators(name) is True, name

    # Real operating companies — keep. "Holdings" is no longer flagged
    # (used to false-positive Safepoint Holdings & similar); "United"
    # must not collide with the "unit" token.
    keep = [
        "Quantinuum Inc.",
        "WhiteHawk Income Corp",
        "INNIO Holding GmbH",
        "Safepoint Holdings, Inc.",
        "Applied Aerospace & Defense, Inc.",
        "United Airlines Holdings",
        "Sunshine Silver Mining & Refining Company",
        "Optimi Health Corp. Common Shares",
        None,
        "",
    ]
    for name in keep:
        assert _etf_trust_indicators(name) is False, name


def test_ipo_country_mapping() -> None:
    assert country_for_exchange("NASDAQ") == "US"
    assert country_for_exchange("HKEX") == "HK"
    assert country_for_exchange("KOSDAQ") == "KR"
    assert country_for_exchange(None) is None
    # Regression: EODHD emits "HKSE" for Hong Kong (not "HKEX"); it was
    # unmapped → None → every HK IPO silently dropped before alerting.
    assert country_for_exchange("HKSE") == "HK"
    assert country_for_exchange("hkse") == "HK"  # case-insensitive
    # Mainland China is now in scope.
    assert country_for_exchange("Shanghai") == "CN"
    assert country_for_exchange("Shenzhen") == "CN"
    assert country_for_exchange("NYSEArca") == "US"


async def test_earnings_sync_matches_only_tracked_and_is_idempotent(
    db_session: AsyncSession,
) -> None:
    db_session.add(
        TrackedCompany(symbol="AAPL", exchange="US", company_name="Apple Inc", source="manual")
    )
    await db_session.commit()

    s1 = await sync_earnings(db_session, adapter=_EarningsStub())
    assert s1.fetched == 3
    assert s1.events_created == 3  # all events persisted
    assert s1.matched == 1  # only AAPL is tracked
    assert s1.notifications_created == 1

    s2 = await sync_earnings(db_session, adapter=_EarningsStub())
    assert s2.events_created == 0  # idempotent upsert
    assert s2.notifications_created == 0  # dedup by source_event_id + window

    events = (await db_session.execute(select(func.count()).select_from(Event))).scalar_one()
    assert events == 3
    notifs = (await db_session.execute(select(func.count()).select_from(Notification))).scalar_one()
    assert notifs == 1


async def test_ipo_sync_keeps_only_us_rows(
    db_session: AsyncSession,
) -> None:
    """EODHD IPO sync is US-only: non-US rows (HK, KR, etc.) are dropped
    at ingest. HK is covered by AAStocks, which has richer schedule data
    and avoids EODHD's HK filing_date == start_date artifact."""
    s1 = await sync_ipos(db_session, adapter=_IpoStub())
    assert s1.fetched == 3
    assert s1.events_created == 1  # only US row persisted
    assert s1.matched == 1
    assert s1.notifications_created == 1

    # HK + Korea rows never become events.
    non_us_names = ["HK Biotech Holdings", "Hangang Microdevices Co"]
    non_us = (
        await db_session.execute(
            select(Event).where(Event.company_name.in_(non_us_names))
        )
    ).scalars().all()
    assert non_us == []

    s2 = await sync_ipos(db_session, adapter=_IpoStub())
    assert s2.events_created == 0
    assert s2.notifications_created == 0

    from catalyst_radar.repositories.event_repository import EventRepository

    repo = EventRepository(db_session)
    all_ipos = await repo.list_events("ipo")
    relevant = await repo.list_events("ipo", relevant=True)
    assert len(all_ipos) == 1
    assert len(relevant) == 1
    assert all(e.country == "US" for e in relevant)
