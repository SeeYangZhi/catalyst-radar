"""Tests for the HKEX newly-listed flip (Phase 9.5b).

Parser: the HKEXnews per-year "New Listing Report" workbook
(NLR<year>_Eng.xlsx) -> (zero-padded 5-digit code, listing date) rows.
Service: HK IPO events whose code appears in the report flip to
``status="listed"`` idempotently, and listed events stop producing
upcoming-IPO reminder notifications.
"""

import io
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.adapters.aastocks_ipo import AastocksIpoAdapter
from catalyst_radar.adapters.base import FetchResult
from catalyst_radar.adapters.hkex_newly_listed import (
    HkexNewlyListedAdapter,
    parse_new_listing_report,
)
from catalyst_radar.config import settings
from catalyst_radar.models.base import today_local, utcnow
from catalyst_radar.models.event import Event
from catalyst_radar.models.notification import Notification
from catalyst_radar.services.hk_ipo_enrich import flip_listed_hk_ipos
from catalyst_radar.services.hk_ipo_sync import sync_hk_ipos

FIXTURE = (Path(__file__).parent / "fixtures" / "hkex_nlr_main_2026.xlsx").read_bytes()


# ── parser ───────────────────────────────────────────────────────────


def test_parse_nlr_fixture_codes_and_dates() -> None:
    rows = parse_new_listing_report(FIXTURE)
    assert rows, "fixture must yield rows"
    by_code = {r["code"]: r for r in rows}
    # First row of the (synthetic) 2026 report: Shanghai Nimbus, listed 2 Jan 2026.
    assert "09801" in by_code
    assert by_code["09801"]["listing_date"] == "2026-01-02"
    assert "Nimbus" in by_code["09801"]["name"]
    # Text-formatted dd/mm/yyyy listing date (unpadded code) still parses.
    assert by_code["09804"]["listing_date"] == "2026-01-09"
    # A row with a code but no listing date is skipped.
    assert "09806" not in by_code
    # Every code is zero-padded to exactly 5 digits and unique.
    assert all(len(c) == 5 and c.isdigit() for c in by_code)
    assert len(by_code) == len(rows)
    # Continuation rows (funds-raised '(b)' lines carry '"' cells) are
    # not emitted as separate listings.
    assert all(r["listing_date"] for r in rows)


def test_parse_malformed_bytes_fail_soft() -> None:
    assert parse_new_listing_report(b"definitely not an xlsx") == []
    assert parse_new_listing_report(b"") == []


def test_parse_zero_yield_returns_empty_list() -> None:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["Some", "Unrelated", "Sheet"])
    ws.append(["1", "2", "3"])
    buf = io.BytesIO()
    wb.save(buf)
    assert parse_new_listing_report(buf.getvalue()) == []


def test_source_event_id_matches_hk_ipo_key() -> None:
    a = HkexNewlyListedAdapter()
    # Same key space as the AAStocks discovery adapter, so the report
    # row identifies the existing hkex:ipo:<code> Event.
    assert a.source_event_id({"code": "1234"}) == "hkex:ipo:01234"
    assert a.source_event_id({"code": "09801"}) == "hkex:ipo:09801"


# ── flip service ─────────────────────────────────────────────────────


class _NewlyListedStub(HkexNewlyListedAdapter):
    """Feeds canned (code, listing_date) rows — no network."""

    def __init__(self, rows: list[dict]) -> None:
        super().__init__()
        self._rows = rows

    async def fetch(self, target: object = None) -> FetchResult:
        return FetchResult(
            source_name=self.source_name,
            schema_name=self.schema_name,
            source_url="stub://nlr",
            http_status=200,
            payload={"rows": self._rows},
            items=list(self._rows),
        )


def _hk_event(code: str, status: str = "new") -> Event:
    return Event(
        event_type="ipo",
        source_name="aastocks",
        source_event_id=f"hkex:ipo:{code}",
        dedup_key=f"hk-{code}",
        symbol=code,
        exchange="HKSE",
        country="HK",
        company_name=f"Co {code}",
        title=f"IPO: Co {code}",
        event_date=utcnow(),
        status=status,
        payload={"code": code},
    )


async def test_flip_marks_listed_and_is_idempotent(db_session: AsyncSession) -> None:
    db_session.add(_hk_event("01234"))
    await db_session.commit()

    stub = _NewlyListedStub([{"code": "01234", "listing_date": "2026-06-10", "name": "Co"}])
    s1 = await flip_listed_hk_ipos(db_session, adapter=stub)
    assert s1.flipped == 1

    ev = (
        await db_session.execute(
            select(Event).where(Event.source_event_id == "hkex:ipo:01234")
        )
    ).scalar_one()
    assert ev.status == "listed"
    assert ev.payload["listing_confirmed"]["listing_date"] == "2026-06-10"
    assert ev.payload["listing_confirmed"]["source"] == "hkexnews.newly_listed"

    # Second run is a no-op: already-listed events are excluded.
    s2 = await flip_listed_hk_ipos(db_session, adapter=stub)
    assert s2.flipped == 0
    await db_session.refresh(ev)
    assert ev.status == "listed"


async def test_flip_unknown_code_no_effect(db_session: AsyncSession) -> None:
    db_session.add(_hk_event("01234"))
    await db_session.commit()

    stub = _NewlyListedStub([{"code": "09999", "listing_date": "2026-06-10", "name": "Other"}])
    s = await flip_listed_hk_ipos(db_session, adapter=stub)
    assert s.flipped == 0

    ev = (
        await db_session.execute(
            select(Event).where(Event.source_event_id == "hkex:ipo:01234")
        )
    ).scalar_one()
    assert ev.status == "new"
    assert "listing_confirmed" not in (ev.payload or {})


async def test_flip_leaves_ignored_events_alone(db_session: AsyncSession) -> None:
    db_session.add(_hk_event("01234", status="ignored"))
    await db_session.commit()

    stub = _NewlyListedStub([{"code": "01234", "listing_date": "2026-06-10", "name": "Co"}])
    s = await flip_listed_hk_ipos(db_session, adapter=stub)
    assert s.flipped == 0

    ev = (
        await db_session.execute(
            select(Event).where(Event.source_event_id == "hkex:ipo:01234")
        )
    ).scalar_one()
    assert ev.status == "ignored"


async def test_flip_disabled(db_session: AsyncSession) -> None:
    from catalyst_radar.repositories.config_repository import ConfigRepository

    await ConfigRepository(db_session).set("hkex_listed_flip_enabled", False)
    await db_session.commit()
    db_session.add(_hk_event("01234"))
    await db_session.commit()

    stub = _NewlyListedStub([{"code": "01234", "listing_date": "2026-06-10", "name": "Co"}])
    s = await flip_listed_hk_ipos(db_session, adapter=stub)
    assert (s.listed_codes, s.flipped) == (0, 0)


async def test_flip_empty_feed_is_noop(db_session: AsyncSession) -> None:
    db_session.add(_hk_event("01234"))
    await db_session.commit()

    s = await flip_listed_hk_ipos(db_session, adapter=_NewlyListedStub([]))
    assert s.flipped == 0
    ev = (
        await db_session.execute(
            select(Event).where(Event.source_event_id == "hkex:ipo:01234")
        )
    ).scalar_one()
    assert ev.status == "new"


# ── reminder suppression ─────────────────────────────────────────────


class _AastocksWindowStub(AastocksIpoAdapter):
    """Feeds canned calendar items — no Playwright/browser."""

    def __init__(self, items: list[dict]) -> None:
        super().__init__()
        self._items = items

    async def fetch(self, target: object = None) -> FetchResult:
        return FetchResult(
            source_name=self.source_name,
            schema_name="aastocks.ipocalendar.v1",
            source_url=self.url,
            http_status=200,
            payload="<html/>",
            items=self._items,
        )


async def test_listed_event_produces_no_reminder(db_session: AsyncSession) -> None:
    """Drive the HK reminder path with one listed and one upcoming IPO,
    both inside the 1-day reminder window: only the upcoming one may
    produce a notification candidate."""
    today = today_local(settings.alert_timezone)
    list_date = (today + timedelta(days=1)).strftime("%Y/%m/%d")
    items = [
        {"code": "06872", "name": "AlreadyTrading", "list_date": list_date},
        {"code": "06999", "name": "StillUpcoming", "list_date": list_date},
    ]
    # 06872 was flipped to listed by a prior flip run.
    db_session.add(_hk_event("06872", status="listed"))
    await db_session.commit()

    s = await sync_hk_ipos(db_session, adapter=_AastocksWindowStub(items))
    assert s.notifications_created == 1

    notifs = list((await db_session.execute(select(Notification))).scalars().all())
    assert len(notifs) == 1
    upcoming = (
        await db_session.execute(
            select(Event).where(Event.source_event_id == "hkex:ipo:06999")
        )
    ).scalar_one()
    assert notifs[0].event_id == upcoming.id

    listed = (
        await db_session.execute(
            select(Event).where(Event.source_event_id == "hkex:ipo:06872")
        )
    ).scalar_one()
    assert listed.status == "listed"  # sync upsert must not resurrect it


async def test_sync_preserves_listing_confirmed_after_flip(db_session: AsyncSession) -> None:
    """Regression: the AAStocks sync's upsert payload refresh must not wipe
    ``payload['listing_confirmed']`` written by a prior flip run."""
    db_session.add(_hk_event("01234"))
    await db_session.commit()

    stub = _NewlyListedStub([{"code": "01234", "listing_date": "2026-06-10", "name": "Co"}])
    s = await flip_listed_hk_ipos(db_session, adapter=stub)
    assert s.flipped == 1

    today = today_local(settings.alert_timezone)
    list_date = (today + timedelta(days=1)).strftime("%Y/%m/%d")
    items = [{"code": "01234", "name": "Co 01234", "list_date": list_date}]
    await sync_hk_ipos(db_session, adapter=_AastocksWindowStub(items))

    ev = (
        await db_session.execute(
            select(Event).where(Event.source_event_id == "hkex:ipo:01234")
        )
    ).scalar_one()
    assert ev.status == "listed"
    assert ev.payload["listing_confirmed"]["listing_date"] == "2026-06-10"
    assert ev.payload["listing_confirmed"]["source"] == "hkexnews.newly_listed"
