"""Characterization suite for services/ipo_sync.py (and the HK twin it
feeds, services/hk_ipo_sync.py): idempotent ingest, cross-source HK
dedupe, reminder-window selection, country gating, and the two keyword
filters.

All adapters are stubbed (no Playwright, no EODHD); rows are built
in-test and date-anchored to "today" in the alert timezone so the
reminder-window assertions can never age out (see tests/AGENTS.md).
"""

from datetime import date, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.adapters.aastocks_ipo import AastocksIpoAdapter
from catalyst_radar.adapters.base import FetchResult
from catalyst_radar.adapters.eodhd_calendar import EodhdIpoAdapter
from catalyst_radar.config import settings
from catalyst_radar.dedup import dedup_key as make_dedup_key
from catalyst_radar.models.base import today_local
from catalyst_radar.models.event import Event, EventRelevance
from catalyst_radar.models.notification import Notification
from catalyst_radar.repositories.config_repository import ConfigRepository
from catalyst_radar.services import ipo_sync
from catalyst_radar.services.hk_ipo_sync import sync_hk_ipos
from catalyst_radar.services.ipo_sync import sync_ipos

TODAY = today_local(settings.alert_timezone)


def _rel_iso(days: int) -> str:
    """ISO date `days` from today — EODHD start_date format."""
    return (TODAY + timedelta(days=days)).isoformat()


def _rel_slash(days: int) -> str:
    """YYYY/MM/DD date `days` from today — AAStocks data-listdate format."""
    return (TODAY + timedelta(days=days)).strftime("%Y/%m/%d")


def _us_row(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "code": "ACME.US",
        "name": "Acme Robotics Inc",
        "exchange": "NASDAQ",
        "currency": "USD",
        "start_date": _rel_iso(5),
        "filing_date": _rel_iso(-30),
        "amended_date": None,
        "price_from": 10.0,
        "price_to": 12.0,
        "offer_price": None,
        "shares": 1_000_000,
        "deal_type": "IPO",
    }
    row.update(over)
    return row


def _hk_row(**over: Any) -> dict[str, Any]:
    """Raw AAStocks calendar item (shape of parse_ipocalendar output)."""
    row: dict[str, Any] = {
        "code": "1234",  # unpadded on purpose — normalization under test
        "name": "Dragon Robotics",
        "list_date": _rel_slash(7),
        "app_open": _rel_slash(-3),
        "app_close": _rel_slash(2),
        "ann_date": _rel_slash(6),
        "list_label": "Listing",
    }
    row.update(over)
    return row


class _EodhdStub(EodhdIpoAdapter):
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        super().__init__(api_key="stub", base_url="https://eodhd.test")
        self._rows = rows

    async def fetch(self, target: tuple[date, date]) -> FetchResult:
        return FetchResult(
            source_name=self.source_name,
            schema_name="eodhd.ipos.v1",
            source_url="https://eodhd.test/ipos",
            http_status=200,
            payload={"ipos": self._rows},
            items=self._rows,
        )


class _AastocksStub(AastocksIpoAdapter):
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        super().__init__()
        self._rows = rows

    async def fetch(self, target: object = None) -> FetchResult:
        return FetchResult(
            source_name=self.source_name,
            schema_name="aastocks.ipocalendar.v1",
            source_url=self.url,
            http_status=200,
            payload="<table>stub</table>",
            items=self._rows,
        )


@pytest.fixture(autouse=True)
def _no_fundamentals_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    """sync_ipos enriches newly created events via a live EODHD
    fundamentals call (`_enrich_description`); pin it off so the suite
    never touches the network. The facet test re-patches it."""

    async def _none(adapter: EodhdIpoAdapter, code: str | None) -> str | None:
        return None

    monkeypatch.setattr(ipo_sync, "_enrich_description", _none)


async def _count(session: AsyncSession, model: type) -> int:
    return (await session.execute(select(func.count()).select_from(model))).scalar_one()


# ── 1. US row → one event, idempotent rerun ─────────────────────────────


async def test_us_ipo_row_creates_one_event_and_rerun_is_idempotent(
    db_session: AsyncSession,
) -> None:
    row = _us_row()
    adapter = _EodhdStub([row])

    s1 = await sync_ipos(db_session, adapter=adapter)
    assert (s1.fetched, s1.events_created, s1.matched, s1.notifications_created) == (1, 1, 1, 1)

    event = (
        await db_session.execute(select(Event).where(Event.symbol == "ACME.US"))
    ).scalar_one()
    # Stable, deterministic source_event_id (exchange:code:start:deal_type).
    expected_sid = f"eodhd:ipo:NASDAQ:ACME.US:{row['start_date']}:IPO"
    assert event.source_event_id == expected_sid
    assert adapter.source_event_id(row) == expected_sid  # stable across calls
    assert event.country == "US"
    assert event.payload["price_from"] == 10.0

    s2 = await sync_ipos(db_session, adapter=adapter)
    assert (s2.events_created, s2.notifications_created) == (0, 0)
    assert await _count(db_session, Event) == 1
    assert await _count(db_session, Notification) == 1


# ── 2. Cross-source HK dedupe with zero-padding ─────────────────────────


async def test_aastocks_and_eodhd_hk_rows_resolve_to_one_event(
    db_session: AsyncSession,
) -> None:
    """AAStocks code `1234` and an EODHD `1234.HK` calendar row are the
    same listing and must yield ONE event. Mechanism (characterized):
    AAStocks keys the event by zero-padded stock code (hkex:ipo:01234);
    sync_ipos drops every non-US row at ingest precisely so EODHD's HK
    rows can never mint a duplicate event for an AAStocks listing."""
    hk = await sync_hk_ipos(db_session, adapter=_AastocksStub([_hk_row(code="1234")]))
    assert hk.events_created == 1

    event = (
        await db_session.execute(select(Event).where(Event.event_type == "ipo"))
    ).scalar_one()
    assert event.source_event_id == "hkex:ipo:01234"  # zero-padded to 5
    assert event.symbol == "01234"
    assert event.country == "HK"

    # Zero-pad equivalence: an already-padded rerun lands on the same event.
    hk2 = await sync_hk_ipos(db_session, adapter=_AastocksStub([_hk_row(code="01234")]))
    assert hk2.events_created == 0

    # EODHD's HK row for the same listing: ingested run succeeds, but the
    # row is skipped (US-only surface) — still exactly one IPO event.
    eodhd_hk = _us_row(
        code="1234.HK",
        name="Dragon Robotics",
        exchange="HKSE",
        currency="HKD",
        start_date=_rel_iso(7),
    )
    us = await sync_ipos(db_session, adapter=_EodhdStub([eodhd_hk]))
    assert (us.fetched, us.events_created, us.matched) == (1, 0, 0)
    assert await _count(db_session, Event) == 1


async def test_each_source_updates_its_own_payload_facet(
    db_session: AsyncSession,
) -> None:
    """AAStocks owns the schedule facet (list_date / app dates); the
    enrichment path owns payload['profile'] via _set_profile. A calendar
    re-sync must refresh its facet WITHOUT clobbering the profile."""
    await sync_hk_ipos(db_session, adapter=_AastocksStub([_hk_row()]))
    event = (
        await db_session.execute(select(Event).where(Event.symbol == "01234"))
    ).scalar_one()
    assert event.payload["list_date"] == _rel_slash(7)

    # Enrichment writes its facet (same setter hk_ipo_enrich/ipo_sync use).
    ipo_sync._set_profile(event, "description", "Builds industrial robots.")
    db_session.add(event)
    await db_session.commit()

    # Calendar moves the listing date → AAStocks facet refreshes…
    await sync_hk_ipos(
        db_session,
        adapter=_AastocksStub([_hk_row(list_date=_rel_slash(9), list_label="Updated")]),
    )
    await db_session.refresh(event)
    assert event.payload["list_date"] == _rel_slash(9)
    assert event.payload["list_label"] == "Updated"
    # …and the enrichment facet survives the upsert payload merge.
    assert event.payload["profile"]["description"] == "Builds industrial robots."


async def test_us_enrichment_profile_survives_calendar_resync(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same facet contract on the US path: the fundamentals description is
    written on create and a later calendar refresh (new offer_price) must
    not wipe it."""

    async def _desc(adapter: EodhdIpoAdapter, code: str | None) -> str | None:
        return "Makes warehouse robots."

    monkeypatch.setattr(ipo_sync, "_enrich_description", _desc)

    await sync_ipos(db_session, adapter=_EodhdStub([_us_row()]))
    event = (
        await db_session.execute(select(Event).where(Event.symbol == "ACME.US"))
    ).scalar_one()
    assert event.payload["profile"]["description"] == "Makes warehouse robots."

    # Pricing finalized upstream; same source_event_id (price not in the key).
    await sync_ipos(db_session, adapter=_EodhdStub([_us_row(offer_price=11.0)]))
    await db_session.refresh(event)
    assert event.payload["offer_price"] == 11.0
    assert event.payload["profile"]["description"] == "Makes warehouse robots."


# ── 3. Reminder windows ─────────────────────────────────────────────────


async def test_reminder_window_inside_fires_with_tightest_window(
    db_session: AsyncSession,
) -> None:
    row = _us_row(start_date=_rel_iso(5))  # windows 14,7,1 → lands in 7
    s = await sync_ipos(db_session, adapter=_EodhdStub([row]))
    assert s.notifications_created == 1

    notif = (await db_session.execute(select(Notification))).scalar_one()
    assert notif.reminder_window == "7"  # tightest window containing +5d
    assert notif.status == "pending"
    assert notif.channel == "telegram"
    sid = f"eodhd:ipo:NASDAQ:ACME.US:{row['start_date']}:IPO"
    assert notif.dedup_key == make_dedup_key(sid, "ipo", "7")
    assert "Acme Robotics" in notif.payload["text"]


async def test_outside_all_windows_creates_no_alert_candidate(
    db_session: AsyncSession,
) -> None:
    rows = [
        _us_row(code="FAR.US", name="Faraway Robotics", start_date=_rel_iso(30)),
        _us_row(code="PAST.US", name="Pastco Robotics", start_date=_rel_iso(-2)),
    ]
    s = await sync_ipos(db_session, adapter=_EodhdStub(rows))
    # Both are relevant (US enabled) but neither is inside 14/7/1.
    assert (s.events_created, s.matched, s.notifications_created) == (2, 2, 0)
    assert await _count(db_session, Notification) == 0


async def test_same_window_never_duplicates_but_next_window_fires(
    db_session: AsyncSession,
) -> None:
    """Window dedup is (source_event_id, window): reruns inside the same
    window are silent; the reminder ladder still fires once per window as
    the date approaches. Driven through the HK sync because the AAStocks
    key is date-independent (the EODHD key embeds start_date)."""
    stub10 = _AastocksStub([_hk_row(list_date=_rel_slash(10))])
    s1 = await sync_hk_ipos(db_session, adapter=stub10)
    assert s1.notifications_created == 1  # +10d → window 14

    s2 = await sync_hk_ipos(db_session, adapter=stub10)
    assert s2.notifications_created == 0  # same window → dedup'd
    assert await _count(db_session, Notification) == 1

    s3 = await sync_hk_ipos(
        db_session, adapter=_AastocksStub([_hk_row(list_date=_rel_slash(1))])
    )
    assert s3.notifications_created == 1  # +1d → window 1, new dedup key
    windows = {
        n.reminder_window
        for n in (await db_session.execute(select(Notification))).scalars()
    }
    assert windows == {"14", "1"}
    assert await _count(db_session, Event) == 1  # still one event throughout


# ── 4. Country gate ─────────────────────────────────────────────────────


async def test_disabled_country_produces_no_alert_candidate(
    db_session: AsyncSession,
) -> None:
    await ConfigRepository(db_session).set("eodhd_ipo_enabled_countries", "HK,CN")

    s = await sync_ipos(db_session, adapter=_EodhdStub([_us_row(start_date=_rel_iso(1))]))
    # Event is still ingested/stored — gating only suppresses alerting.
    assert (s.events_created, s.matched, s.notifications_created) == (1, 0, 0)
    assert await _count(db_session, Event) == 1
    assert await _count(db_session, EventRelevance) == 0
    assert await _count(db_session, Notification) == 0


# ── 5. Sector / keyword filters ─────────────────────────────────────────


async def test_sector_keywords_config_filters_on_company_name(
    db_session: AsyncSession,
) -> None:
    """Legacy `ipo_sector_keywords` app_config list: company-name substring
    match; misses are stored but never become alert candidates."""
    await ConfigRepository(db_session).set("ipo_sector_keywords", ["robot"])

    rows = [
        _us_row(code="ACME.US", name="Acme Robotics Inc", start_date=_rel_iso(5)),
        _us_row(code="BLU.US", name="Bluewater Beverages Inc", start_date=_rel_iso(5)),
    ]
    s = await sync_ipos(db_session, adapter=_EodhdStub(rows))
    assert (s.events_created, s.matched, s.notifications_created) == (2, 1, 1)

    relevance = (await db_session.execute(select(EventRelevance))).scalars().all()
    assert len(relevance) == 1
    matched_event = await db_session.get(Event, relevance[0].event_id)
    assert matched_event.symbol == "ACME.US"


async def test_industry_keyword_filter_respected(db_session: AsyncSession) -> None:
    """Runtime `ipo_industry_keywords` filter (_industry_matches): matches
    against company name + profile description; non-matching IPOs are
    ingested but produce no relevance row and no notification."""
    await ConfigRepository(db_session).set("ipo_industry_keywords", "semiconductor")

    rows = [
        _us_row(code="CHIP.US", name="Acme Semiconductor Corp", start_date=_rel_iso(5)),
        _us_row(code="BLU.US", name="Bluewater Beverages Inc", start_date=_rel_iso(5)),
    ]
    s = await sync_ipos(db_session, adapter=_EodhdStub(rows))
    assert (s.events_created, s.matched, s.notifications_created) == (2, 1, 1)

    notif = (await db_session.execute(select(Notification))).scalar_one()
    event = await db_session.get(Event, notif.event_id)
    assert event.symbol == "CHIP.US"
