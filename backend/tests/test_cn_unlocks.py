"""Tests for the tushare share_float pipeline: adapter normalization,
window grouping, importance-from-ratio, sync end-to-end with a stubbed
adapter (no live tushare call)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.adapters.tushare_unlocks import (
    TushareUnlocksAdapter,
    UnlockRow,
    _parse_date,
    _to_ts_code,
    group_upcoming,
    importance_for_ratio,
)


def test_to_ts_code_maps_sse_szse_and_rejects_bse() -> None:
    assert _to_ts_code("600519", "SSE") == "600519.SH"
    assert _to_ts_code("688635", "SSE") == "688635.SH"  # STAR
    assert _to_ts_code("000001", "SZSE") == "000001.SZ"
    assert _to_ts_code("301669", "SZSE") == "301669.SZ"  # ChiNext
    # BSE ticker — adapter declines (no tushare 北交所 support in our scope).
    assert _to_ts_code("920218", "BSE") is None
    # Malformed.
    assert _to_ts_code("abc", "SSE") is None
    assert _to_ts_code("12345", "SSE") is None  # 5-digit, not 6
    assert _to_ts_code("", "SSE") is None


def test_parse_date_handles_tushare_yyyymmdd_strings() -> None:
    assert _parse_date("20260525") == date(2026, 5, 25)
    assert _parse_date(20260525) == date(2026, 5, 25)
    assert _parse_date(None) is None
    assert _parse_date("") is None
    assert _parse_date("nan") is None
    assert _parse_date(float("nan")) is None
    assert _parse_date("garbage") is None


def test_importance_thresholds() -> None:
    assert importance_for_ratio(7.5, high_threshold=5.0, medium_threshold=1.0) == "high"
    assert importance_for_ratio(5.0, high_threshold=5.0, medium_threshold=1.0) == "high"
    assert importance_for_ratio(2.5, high_threshold=5.0, medium_threshold=1.0) == "medium"
    assert importance_for_ratio(0.4, high_threshold=5.0, medium_threshold=1.0) == "low"


def test_group_upcoming_aggregates_by_date_and_drops_past() -> None:
    today = date(2026, 5, 25)
    rows = [
        # Past — must drop.
        UnlockRow(
            ts_code="600519.SH",
            ann_date=date(2025, 5, 1),
            float_date=date(2025, 6, 1),
            float_share=1_000_000,
            float_ratio=0.5,
            holder_name="Old Holder",
            share_type="股权分置限售股份",
        ),
        # Inside window, two holders same date.
        UnlockRow(
            ts_code="600519.SH",
            ann_date=date(2026, 5, 1),
            float_date=date(2026, 6, 1),
            float_share=5_000_000,
            float_ratio=2.5,
            holder_name="Holder A",
            share_type="首发限售股份",
        ),
        UnlockRow(
            ts_code="600519.SH",
            ann_date=date(2026, 5, 1),
            float_date=date(2026, 6, 1),
            float_share=3_000_000,
            float_ratio=1.5,
            holder_name="Holder B",
            share_type="首发限售股份",
        ),
        # Outside window (> 30 days out) — must drop.
        UnlockRow(
            ts_code="600519.SH",
            ann_date=date(2026, 5, 1),
            float_date=date(2026, 10, 1),
            float_share=100_000,
            float_ratio=0.1,
            holder_name="Far Holder",
            share_type="首发限售股份",
        ),
    ]
    grouped = group_upcoming(
        rows, symbol="600519", exchange="SSE", today=today, lookahead_days=30
    )
    assert len(grouped) == 1
    g = grouped[0]
    assert g.float_date == date(2026, 6, 1)
    assert g.total_share == 8_000_000
    assert g.total_ratio == 4.0
    assert len(g.holders) == 2
    assert g.primary_share_type == "首发限售股份"


# ── sync end-to-end with a stubbed tushare adapter ──────────────────


class _UnlocksStub(TushareUnlocksAdapter):
    """In-memory tushare stand-in. Returns canned rows per ticker —
    enough to drive the sync, classification, and notification paths."""

    def __init__(self, rows_by_symbol: dict[str, list[UnlockRow]]) -> None:
        super().__init__(api_token="stub-token")
        self._rows = rows_by_symbol

    @property
    def configured(self) -> bool:  # type: ignore[override]
        return True

    async def fetch_unlocks(self, *, symbol: str, exchange: str) -> list[UnlockRow]:  # type: ignore[override]
        return self._rows.get(symbol, [])


async def test_sync_cn_unlocks_creates_events_and_notifications(
    db_session: AsyncSession,
) -> None:
    from catalyst_radar.models.company import TrackedCompany
    from catalyst_radar.models.event import Event
    from catalyst_radar.models.notification import Notification
    from catalyst_radar.services.cn_unlocks_sync import sync_cn_unlocks

    # Two CN companies — one with an in-window unlock, one with none.
    db_session.add_all(
        [
            TrackedCompany(
                symbol="600519", exchange="SSE", company_name="Kweichow Moutai", source="manual"
            ),
            TrackedCompany(
                symbol="000001", exchange="SZSE", company_name="Ping An Bank", source="manual"
            ),
            # Non-CN — must NOT call tushare for these.
            TrackedCompany(symbol="AAPL", exchange="US", company_name="Apple", source="manual"),
        ]
    )
    await db_session.commit()

    today = datetime.now(UTC).date()
    near_unlock = today + timedelta(days=5)  # inside the T-30/T-14/T-7/T-3 windows

    rows = {
        "600519": [
            UnlockRow(
                ts_code="600519.SH",
                ann_date=today - timedelta(days=14),
                float_date=near_unlock,
                float_share=20_000_000,
                float_ratio=8.0,  # > high threshold → importance=high
                holder_name="Major Holder",
                share_type="首发限售股份",
            ),
        ],
        # 000001 → no rows = no unlocks. Sync still has to handle this
        # without erroring.
    }
    s = await sync_cn_unlocks(db_session, adapter=_UnlocksStub(rows))
    assert s.companies_checked == 2  # SSE + SZSE, US ignored
    assert s.upcoming_total == 1
    assert s.events_created == 1
    # T-5 falls inside all of {30, 14, 7} windows; pick_window picks the
    # nearest qualifying = 7.
    assert s.notifications_created == 1
    assert s.errors == 0

    # Verify the persisted Event carries the unlock payload + classification.
    e = (await db_session.execute(select(Event).where(Event.symbol == "600519"))).scalar_one()
    assert e.country == "CN"
    assert e.event_type == "catalyst"
    assert e.payload["classification"]["event_subtype"] == "lockup_unlock"
    assert e.payload["classification"]["importance"] == "high"
    assert e.payload["unlock"]["total_ratio"] == 8.0
    assert e.payload["unlock"]["holders"][0]["name"] == "Major Holder"

    # Idempotent — re-run creates no new events or notifications.
    s2 = await sync_cn_unlocks(db_session, adapter=_UnlocksStub(rows))
    assert s2.events_created == 0
    assert s2.notifications_created == 0
    notifs = (await db_session.execute(select(func.count()).select_from(Notification))).scalar_one()
    assert notifs == 1


async def test_sync_disabled_when_no_token(db_session: AsyncSession) -> None:
    """No token → skip cleanly without contacting tushare."""
    from catalyst_radar.services.cn_unlocks_sync import sync_cn_unlocks

    adapter = TushareUnlocksAdapter(api_token="")
    assert not adapter.configured
    s = await sync_cn_unlocks(db_session, adapter=adapter)
    assert s.companies_checked == 0
    assert s.upcoming_total == 0
    assert s.events_created == 0
    assert s.notifications_created == 0


async def test_sync_disabled_when_runtime_flag_off(db_session: AsyncSession) -> None:
    """Runtime flag off (UI toggle) → skip cleanly even with a valid token."""
    from catalyst_radar.models.company import TrackedCompany
    from catalyst_radar.repositories.config_repository import ConfigRepository
    from catalyst_radar.services.cn_unlocks_sync import sync_cn_unlocks

    db_session.add(
        TrackedCompany(symbol="600519", exchange="SSE", company_name="Moutai", source="manual")
    )
    await ConfigRepository(db_session).set("cn_unlocks_enabled", False)
    await db_session.commit()
    s = await sync_cn_unlocks(db_session, adapter=_UnlocksStub({}))
    assert s.companies_checked == 0
    assert s.events_created == 0
