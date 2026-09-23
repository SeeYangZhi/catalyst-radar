"""Tests for the CN IPO review-stage pipeline.

Covers the akshare wrapper (no network — stubbed DataFrame), the sync
service against an in-memory session, and the alert formatter. The
DataFrame fixture mirrors the real Eastmoney columns observed against
``ak.stock_ipo_review_em()``: 上交所主板 / 上交所科创板 / 深交所创业板 /
北交所 plus the five 审核状态 values."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.adapters.akshare_ipo_review import (
    REVIEW_CALENDAR_URL,
    AkshareIpoReviewAdapter,
    eastmoney_search_url,
    normalize_rows,
)


def _today() -> pd.Timestamp:
    return pd.Timestamp(datetime.now(UTC).date())


def _make_review_df() -> pd.DataFrame:
    """Mimic ``ak.stock_ipo_review_em()`` output. The two flagship rows
    (CXMT, Unitree) are dated in the recent past so they land inside the
    alert window. The BSE and the 100-days-old rows exist to assert the
    out-of-scope filters."""
    today = _today()
    return pd.DataFrame(
        [
            {
                "序号": 1,
                "企业名称": "宇树科技股份有限公司",
                "股票简称": "宇树科技",
                "股票代码": "A26029",
                "上市板块": "上交所科创板",
                "上会日期": today - pd.Timedelta(days=1),
                "审核状态": "上会通过",
                "发审委委员": "马义涛,王晗",
                "主承销商": "中信证券",
                "发行数量(股)": 40_446_434,
                "拟融资额(元)": 4.2017e9,
                "公告日期": today - pd.Timedelta(days=70),
                "上市日期": pd.NaT,
            },
            {
                "序号": 2,
                "企业名称": "长鑫科技集团股份有限公司",
                "股票简称": "长鑫科技",
                "股票代码": "A25310",
                "上市板块": "上交所科创板",
                "上会日期": today - pd.Timedelta(days=5),
                "审核状态": "上会通过",
                "发审委委员": "...",
                "主承销商": "中金公司",
                "发行数量(股)": 200_000_000,
                "拟融资额(元)": 8.0e9,
                "公告日期": today - pd.Timedelta(days=60),
                "上市日期": pd.NaT,
            },
            {
                "序号": 3,
                "企业名称": "广东中塑新材料股份有限公司",
                "股票简称": "中塑股份",
                "股票代码": "A25219",
                "上市板块": "深交所创业板",
                "上会日期": today + pd.Timedelta(days=3),
                "审核状态": "未上会",
                "发审委委员": "...",
                "主承销商": "国信证券",
                "发行数量(股)": 12_332_900,
                "拟融资额(元)": 6.45e8,
                "公告日期": today - pd.Timedelta(days=10),
                "上市日期": pd.NaT,
            },
            {
                "序号": 4,
                "企业名称": "北京贝尔生物工程股份有限公司",
                "股票简称": "贝尔生物",
                "股票代码": "A25076",
                "上市板块": "北交所",
                "上会日期": today - pd.Timedelta(days=2),
                "审核状态": "上会通过",
                "发审委委员": "...",
                "主承销商": "国泰海通证券",
                "发行数量(股)": 25_000_000,
                "拟融资额(元)": 3.6e7,
                "公告日期": today - pd.Timedelta(days=120),
                "上市日期": pd.NaT,
            },
            {
                "序号": 5,
                "企业名称": "陈年公司股份有限公司",
                "股票简称": "陈年公司",
                "股票代码": "A20100",
                "上市板块": "上交所主板",
                "上会日期": today - pd.Timedelta(days=200),  # outside LOOKBACK_DAYS
                "审核状态": "上会通过",
                "发审委委员": "...",
                "主承销商": "中信证券",
                "发行数量(股)": 10_000_000,
                "拟融资额(元)": 1.0e8,
                "公告日期": today - pd.Timedelta(days=400),
                "上市日期": pd.NaT,
            },
        ]
    )


class _ReviewStub(AkshareIpoReviewAdapter):
    @staticmethod
    def _fetch_blocking() -> pd.DataFrame:  # type: ignore[override]
        return _make_review_df()


# ── adapter ─────────────────────────────────────────────────────────


async def test_review_adapter_normalize_filters_and_classifies() -> None:
    adapter = _ReviewStub()
    result = await adapter.fetch()
    assert result.http_status == 200
    codes = {it["code"] for it in result.items}
    # A20100 is outside the LOOKBACK_DAYS window and must be dropped at
    # adapter level; the rest survive.
    assert codes == {"A26029", "A25310", "A25219", "A25076"}
    by_code = {it["code"]: it for it in result.items}
    assert by_code["A26029"]["status"] == "approved"
    assert by_code["A26029"]["exchange"] == "SSE"
    assert by_code["A25076"]["exchange"] == "BSE"
    assert by_code["A25219"]["status"] == "scheduled"


def test_review_adapter_source_event_id_encodes_status() -> None:
    a = AkshareIpoReviewAdapter()
    sid1 = a.source_event_id({"code": "A26029", "status": "scheduled"})
    sid2 = a.source_event_id({"code": "A26029", "status": "approved"})
    # A status transition must yield a fresh source_event_id so the
    # upsert creates a new event row and the user gets a new alert.
    assert sid1 != sid2
    assert sid1.endswith(":scheduled")
    assert sid2.endswith(":approved")


def test_review_adapter_normalize_payload_shape() -> None:
    rows = normalize_rows(_make_review_df())
    a = AkshareIpoReviewAdapter()
    cxmt = next(r for r in rows if r["code"] == "A25310")
    norm = a.normalize(cxmt)
    p = norm["payload"]
    assert p["stage"] == "review"
    assert p["status"] == "approved"
    assert p["status_cn"] == "上会通过"
    assert p["board"] == "上交所科创板"
    assert p["underwriter"] == "中金公司"
    assert p["deal_size_cny"] == 8.0e9
    assert norm["event_type"] == "ipo"
    assert norm["title"].startswith("IPO review (approved):")


# ── sync ────────────────────────────────────────────────────────────


async def test_sync_cn_ipo_review_creates_alerts_for_all_mainland_boards(
    db_session: AsyncSession,
) -> None:
    from catalyst_radar.models.event import Event
    from catalyst_radar.models.notification import Notification
    from catalyst_radar.services.cn_ipo_review_sync import sync_cn_ipo_review

    s1 = await sync_cn_ipo_review(db_session, adapter=_ReviewStub())
    # All four mainland-board rows survive the LOOKBACK_DAYS filter and
    # alert — SSE/SZSE/BSE are all in scope.
    assert s1.fetched == 4
    assert s1.events_created == 4
    assert s1.matched == 4
    assert s1.notifications_created == 4

    cxmt = (
        await db_session.execute(select(Event).where(Event.symbol == "A25310"))
    ).scalar_one()
    assert cxmt.country == "CN"
    assert cxmt.payload["stage"] == "review"
    assert cxmt.payload["status"] == "approved"
    # The event's link is the per-company Eastmoney search (built from the
    # 股票简称), not the shared committee calendar.
    assert cxmt.source_url == eastmoney_search_url("长鑫科技")
    assert cxmt.source_url != REVIEW_CALENDAR_URL

    bse = (
        await db_session.execute(select(Event).where(Event.symbol == "A25076"))
    ).scalar_one()
    assert bse.exchange == "BSE"
    assert bse.country == "CN"
    # BSE row alerts too.
    bse_notifs = (
        await db_session.execute(
            select(func.count()).select_from(Notification)
            .where(Notification.event_id == bse.id)
        )
    ).scalar_one()
    assert bse_notifs == 1


async def test_sync_cn_ipo_review_is_idempotent(db_session: AsyncSession) -> None:
    from catalyst_radar.models.notification import Notification
    from catalyst_radar.services.cn_ipo_review_sync import sync_cn_ipo_review

    s1 = await sync_cn_ipo_review(db_session, adapter=_ReviewStub())
    s2 = await sync_cn_ipo_review(db_session, adapter=_ReviewStub())
    assert s2.events_created == 0
    assert s2.notifications_created == 0
    notifs_total = (
        await db_session.execute(select(func.count()).select_from(Notification))
    ).scalar_one()
    assert notifs_total == s1.notifications_created


async def test_sync_cn_ipo_review_status_transition_creates_new_alert(
    db_session: AsyncSession,
) -> None:
    """When a row's 审核状态 changes (e.g. 未上会 → 上会通过) a *new*
    event row is created (different source_event_id) and a fresh alert
    fires. This is the user's headline use case."""
    from catalyst_radar.models.event import Event
    from catalyst_radar.services.cn_ipo_review_sync import sync_cn_ipo_review

    today = _today()

    class _ScheduledStub(AkshareIpoReviewAdapter):
        @staticmethod
        def _fetch_blocking() -> pd.DataFrame:
            return pd.DataFrame(
                [
                    {
                        "序号": 1,
                        "企业名称": "测试公司",
                        "股票简称": "测试",
                        "股票代码": "A99999",
                        "上市板块": "上交所主板",
                        "上会日期": today + pd.Timedelta(days=2),
                        "审核状态": "未上会",
                        "发审委委员": "",
                        "主承销商": "中信证券",
                        "发行数量(股)": 1_000_000,
                        "拟融资额(元)": 1.0e7,
                        "公告日期": today - pd.Timedelta(days=10),
                        "上市日期": pd.NaT,
                    }
                ]
            )

    class _ApprovedStub(AkshareIpoReviewAdapter):
        @staticmethod
        def _fetch_blocking() -> pd.DataFrame:
            df = _ScheduledStub._fetch_blocking()
            df.loc[0, "审核状态"] = "上会通过"
            return df

    s1 = await sync_cn_ipo_review(db_session, adapter=_ScheduledStub())
    s2 = await sync_cn_ipo_review(db_session, adapter=_ApprovedStub())

    assert s1.notifications_created == 1
    assert s2.notifications_created == 1  # the approval is a fresh alert
    rows = (
        await db_session.execute(select(Event).where(Event.symbol == "A99999"))
    ).scalars().all()
    statuses = sorted(r.payload["status"] for r in rows)
    assert statuses == ["approved", "scheduled"]


async def test_sync_cn_ipo_review_disabled(db_session: AsyncSession) -> None:
    from catalyst_radar.repositories.config_repository import ConfigRepository
    from catalyst_radar.services.cn_ipo_review_sync import sync_cn_ipo_review

    await ConfigRepository(db_session).set("cn_ipo_review_enabled", False)
    await db_session.commit()
    out = await sync_cn_ipo_review(db_session, adapter=_ReviewStub())
    assert (out.fetched, out.events_created, out.notifications_created) == (0, 0, 0)


# ── alert formatter ─────────────────────────────────────────────────


def test_eastmoney_search_url_encodes_company_name() -> None:
    url = eastmoney_search_url("长鑫科技")
    assert url.startswith("https://so.eastmoney.com/web/s?keyword=")
    # Non-ASCII names must be percent-encoded so the link is valid.
    assert "长鑫科技" not in url
    assert url == "https://so.eastmoney.com/web/s?keyword=" + "%E9%95%BF%E9%91%AB%E7%A7%91%E6%8A%80"


def test_eastmoney_search_url_falls_back_to_calendar_when_blank() -> None:
    # No name to search → the shared committee calendar is the only link.
    assert eastmoney_search_url("") == REVIEW_CALENDAR_URL
    assert eastmoney_search_url("   ") == REVIEW_CALENDAR_URL


def test_format_ipo_review_renders_status_and_money() -> None:
    from catalyst_radar.models.event import Event
    from catalyst_radar.services.alerts import format_ipo

    today = datetime.now(UTC)
    event = Event(
        event_type="ipo",
        source_name="akshare_ipo_review",
        source_event_id="cn_review:A25310:approved",
        dedup_key="dummy",
        symbol="A25310",
        exchange="SSE",
        country="CN",
        company_name="长鑫科技",
        title="IPO review (approved): 长鑫科技",
        event_date=today - timedelta(days=5),
        payload={
            "stage": "review",
            "status": "approved",
            "status_cn": "上会通过",
            "board": "上交所科创板",
            "underwriter": "中金公司",
            "deal_size_cny": 8.0e9,
            "code": "A25310",
            "short_name": "长鑫科技",
            "meeting_date": (today - timedelta(days=5)).date().isoformat(),
        },
        source_url=eastmoney_search_url("长鑫科技"),
    )
    out = format_ipo(event)
    assert "<b>长鑫科技</b>" in out
    assert "<code>A25310</code>" in out
    assert "CSRC review: Approved" in out
    assert "Hearing" in out
    assert "上交所科创板" in out
    assert "中金公司" in out
    assert "¥8.00B" in out
    # The footer link points at the per-company Eastmoney search, relabeled
    # from the old (generic, misleading) "CSRC review calendar".
    assert '<a href="https://so.eastmoney.com/web/s?keyword=' in out
    assert "Company on Eastmoney</a>" in out
    assert "CSRC review calendar" not in out
    # The listing-stage codepath must not run for review events
    # ("Lists" only appears in format_ipo's main branch).
    assert "Lists" not in out


# ── provider resilience (Option B) ──────────────────────────────────


def test_fetch_blocking_rides_out_repeated_chunked_errors(monkeypatch) -> None:
    """Eastmoney's chunked stream can truncate mid-pagination for a few
    minutes; the adapter must retry enough times to outlast a multi-attempt
    transient window. The old 3-attempt policy gave up too early and recorded
    an `empty` run that tripped the stale watchdog."""
    import akshare as ak

    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)  # no real backoff
    calls = {"n": 0}

    class ChunkedEncodingError(Exception):
        """Name-matched as transient by the shared retry classifier."""

    def flaky() -> pd.DataFrame:
        calls["n"] += 1
        if calls["n"] < 5:
            raise ChunkedEncodingError("Response ended prematurely")
        return pd.DataFrame()

    monkeypatch.setattr(ak, "stock_ipo_review_em", flaky)

    df = AkshareIpoReviewAdapter._fetch_blocking()
    # Succeeded only on the 5th attempt — the old 3-attempt policy would have
    # raised and fail-softed to an empty result.
    assert calls["n"] == 5
    assert isinstance(df, pd.DataFrame)


def test_format_ipo_review_renders_profile_description() -> None:
    """CSRC review alerts were going out without the company description
    even when cn_ipo_enrich had already filled payload['profile'] — the
    review-stage card never rendered it. Regression: the description
    blockquote (and websearch citations) must appear on review cards."""
    from catalyst_radar.models.event import Event
    from catalyst_radar.services.alerts import format_ipo

    event = Event(
        event_type="ipo",
        source_name="akshare_ipo_review",
        source_event_id="cn_review:A26029:approved",
        dedup_key="dummy2",
        symbol="A26029",
        exchange="SSE",
        country="CN",
        company_name="宇树科技",
        title="IPO review (approved): 宇树科技",
        event_date=datetime.now(UTC),
        payload={
            "stage": "review",
            "status": "approved",
            "code": "A26029",
            "profile": {
                "description": "全球领先的四足与人形机器人公司。",
                "description_source": "websearch",
                "description_sources": [
                    {"name": "Reuters", "url": "https://reuters.example/unitree"}
                ],
            },
        },
    )
    out = format_ipo(event)
    assert "CSRC review: Approved" in out
    assert "<blockquote>全球领先的四足与人形机器人公司。</blockquote>" in out
    assert '<a href="https://reuters.example/unitree">Reuters</a>' in out
