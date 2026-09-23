from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.adapters.aastocks_ipo import (
    AastocksIpoAdapter,
    parse_ipocalendar,
)
from catalyst_radar.adapters.base import FetchResult
from catalyst_radar.models.event import Event
from catalyst_radar.models.notification import Notification
from catalyst_radar.services.hk_ipo_sync import sync_hk_ipos

FIXTURE = (Path(__file__).parent / "fixtures" / "aastocks_ipocalendar.html").read_text()


class _AastocksStub(AastocksIpoAdapter):
    """Feeds the saved rendered calendar — no Playwright/browser."""

    def __init__(self, html: str = FIXTURE) -> None:
        super().__init__()
        self._html = html

    async def fetch(self, target: object = None) -> FetchResult:
        return FetchResult(
            source_name=self.source_name,
            schema_name="aastocks.ipocalendar.v1",
            source_url=self.url,
            http_status=200,
            payload=self._html,
            items=parse_ipocalendar(self._html),
        )


def test_parse_ipocalendar_fixture() -> None:
    rows = parse_ipocalendar(FIXTURE)
    by_code = {r["code"]: r for r in rows}
    assert by_code["09871"]["name"] == "Kestrel Robotics"
    assert by_code["09871"]["list_date"] == "2026/05/18"
    assert by_code["09511"]["name"] == "Harbourline Mobility"
    assert by_code["09872"]["list_date"] == "2026/05/22"
    # Codes are unique (calendar repeats a code across cells; richest wins).
    assert len(by_code) == len(rows)


def test_normalize_and_stable_id() -> None:
    a = AastocksIpoAdapter()
    raw = {"code": "9871", "name": "Kestrel Robotics", "list_date": "2026/05/18"}
    norm = a.normalize(raw)
    assert norm["symbol"] == "09871"
    assert norm["exchange"] == "HKSE"
    assert norm["event_date"].date().isoformat() == "2026-05-18"
    sid = a.source_event_id(raw)
    assert sid == "hkex:ipo:09871"
    assert a.source_event_id({"code": "09871"}) == sid  # zero-pad equivalence


async def test_sync_hk_ipos_matches_and_is_idempotent(
    db_session: AsyncSession,
) -> None:
    s1 = await sync_hk_ipos(db_session, adapter=_AastocksStub())
    assert s1.fetched == 7
    assert s1.events_created == 7  # one Event per unique stock code
    assert s1.matched == 7  # all HK, HK enabled by default (US,HK,CN)

    rp = (await db_session.execute(select(Event).where(Event.symbol == "09871"))).scalar_one()
    assert rp.country == "HK"
    assert rp.exchange == "HKSE"
    assert rp.source_event_id == "hkex:ipo:09871"

    s2 = await sync_hk_ipos(db_session, adapter=_AastocksStub())
    assert s2.events_created == 0  # idempotent upsert by source_event_id
    assert s2.notifications_created == 0  # dedup by code + window

    events = (await db_session.execute(select(func.count()).select_from(Event))).scalar_one()
    assert events == 7
    # Notifications are only created for not-yet-listed IPOs inside a
    # reminder window; never duplicated across runs.
    notifs = (await db_session.execute(select(func.count()).select_from(Notification))).scalar_one()
    assert notifs == s1.notifications_created


async def test_sync_hk_ipos_disabled(db_session: AsyncSession) -> None:
    from catalyst_radar.repositories.config_repository import ConfigRepository

    await ConfigRepository(db_session).set("hk_ipo_source_aastocks_enabled", False)
    await db_session.commit()
    out = await sync_hk_ipos(db_session, adapter=_AastocksStub())
    assert (out.fetched, out.events_created, out.matched) == (0, 0, 0)
