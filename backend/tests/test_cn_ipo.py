"""Tests for the SSE+SZSE IPO pipeline: akshare adapter, CNINFO adapter,
section-aware prospectus extractor, sync service, enrichment service.

The adapters stub out their respective network calls — akshare via a
DataFrame fixture, CNINFO via a captured JSON envelope, the extractor
via small synthesized PDFs. No live HTTP. Live smoke is done separately
in CI / manually via the production sync."""

from __future__ import annotations

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.adapters.akshare_ipo import AkshareIpoAdapter, normalize_rows


def _make_eastmoney_df() -> pd.DataFrame:
    """Mimic the columns akshare.stock_xgsglb_em returns. Includes one
    SSE row, one SZSE row, one ChiNext row, and one BSE row — all four
    are kept; the adapter covers all mainland boards."""
    return pd.DataFrame(
        [
            {
                "股票代码": "688635",
                "股票简称": "长进光子",
                "申购代码": "787635",
                "交易所": "上海证券交易所",
                "板块": "科创板",
                "发行总数": 33_333_334,
                "网上发行": 16_800_000,
                "顶格申购需配市值": 16.5,
                "申购上限": 16500,
                "发行价格": 40.98,
                "最新价": float("nan"),
                "首日收盘价": float("nan"),
                "申购日期": pd.Timestamp("2026-05-18"),
                "中签号公布日": pd.Timestamp("2026-05-22"),
                "中签缴款日期": pd.Timestamp("2026-05-22"),
                "上市日期": pd.NaT,
                "发行市盈率": 60.50,
                "行业市盈率": 29.13,
                "中签率": 0.0258,
            },
            {
                "股票代码": "301669",
                "股票简称": "高特电子",
                "申购代码": "301669",
                "交易所": "深圳证券交易所",
                "板块": "非科创板",
                "发行总数": 12_000_000,
                "网上发行": 8_400_000,
                "顶格申购需配市值": 16.5,
                "申购上限": 16500,
                "发行价格": float("nan"),
                "最新价": float("nan"),
                "首日收盘价": float("nan"),
                "申购日期": pd.Timestamp("2026-05-29"),
                "中签号公布日": pd.Timestamp("2026-06-02"),
                "中签缴款日期": pd.Timestamp("2026-06-02"),
                "上市日期": pd.NaT,
                "发行市盈率": float("nan"),
                "行业市盈率": 29.13,
                "中签率": float("nan"),
            },
            {
                "股票代码": "001237",
                "股票简称": "惠康科技",
                "申购代码": "001237",
                "交易所": "深圳证券交易所",
                "板块": "非科创板",
                "发行总数": 25_000_000,
                "网上发行": 17_500_000,
                "顶格申购需配市值": 16.5,
                "申购上限": 16500,
                "发行价格": 53.26,
                "最新价": 78.10,
                "首日收盘价": 80.00,
                "申购日期": pd.Timestamp("2026-05-13"),
                "中签号公布日": pd.Timestamp("2026-05-17"),
                "中签缴款日期": pd.Timestamp("2026-05-17"),
                "上市日期": pd.Timestamp("2026-05-22"),
                "发行市盈率": 22.90,
                "行业市盈率": 18.50,
                "中签率": 0.043,
            },
            # BSE row — kept (北交所 is in scope).
            {
                "股票代码": "920218",
                "股票简称": "新天力",
                "申购代码": "920218",
                "交易所": "北京证券交易所",
                "板块": "北交所",
                "发行总数": 2_341,
                "网上发行": 21_076,
                "顶格申购需配市值": 1284,
                "申购上限": 1053800,
                "发行价格": 12.19,
                "最新价": float("nan"),
                "首日收盘价": float("nan"),
                "申购日期": pd.Timestamp("2026-05-20"),
                "中签号公布日": pd.Timestamp("2026-05-25"),
                "中签缴款日期": pd.NaT,
                "上市日期": pd.NaT,
                "发行市盈率": 14.78,
                "行业市盈率": 30.67,
                "中签率": 0.0258,
            },
        ]
    )


class _AkshareStub(AkshareIpoAdapter):
    """No-network akshare adapter for tests."""

    def __init__(self, df: pd.DataFrame | None = None) -> None:
        super().__init__()
        self._df = df if df is not None else _make_eastmoney_df()

    @staticmethod
    def _fetch_blocking() -> pd.DataFrame:  # type: ignore[override]
        return _make_eastmoney_df()


# ── adapter: akshare ────────────────────────────────────────────────


async def test_akshare_normalize_keeps_all_mainland_boards() -> None:
    a = _AkshareStub()
    result = await a.fetch()
    assert result.http_status == 200
    codes = {it["code"] for it in result.items}
    # All four mainland-CN rows kept — SSE STAR, SZSE main, ChiNext, BSE.
    assert codes == {"688635", "301669", "001237", "920218"}
    by_code = {it["code"]: it for it in result.items}
    assert by_code["688635"]["exchange"] == "SSE"
    assert by_code["688635"]["board"] == "STAR"
    assert by_code["301669"]["exchange"] == "SZSE"
    assert by_code["301669"]["board"] == "ChiNext"
    assert by_code["001237"]["exchange"] == "SZSE"
    assert by_code["001237"]["board"] == "SZSE main"
    assert by_code["920218"]["exchange"] == "BSE"
    assert by_code["920218"]["board"] == "BSE"


async def test_akshare_normalize_event_shape() -> None:
    a = _AkshareStub()
    res = await a.fetch()
    raw = next(it for it in res.items if it["code"] == "001237")
    norm = a.normalize(raw)
    assert norm["event_type"] == "ipo"
    assert norm["symbol"] == "001237"
    assert norm["exchange"] == "SZSE"
    # Listing date present → use it.
    assert norm["event_date"].date().isoformat() == "2026-05-22"


async def test_akshare_normalize_falls_back_to_subscription_date() -> None:
    """When listing_date is NaN (the upcoming-IPO case before the listing
    date is finalized), the subscription date drives the alert windows.
    Without this fallback every upcoming row got dropped silently because
    the alert-window logic skips events with event_date is None."""
    a = _AkshareStub()
    res = await a.fetch()
    # 301669 高特电子: listing_date=NaT, subscription_date=2026-05-29
    raw = next(it for it in res.items if it["code"] == "301669")
    norm = a.normalize(raw)
    assert norm["event_date"] is not None
    assert norm["event_date"].date().isoformat() == "2026-05-29"


async def test_akshare_normalize_event_shape_extras() -> None:
    """Payload + source_event_id sanity on a listed row."""
    a = _AkshareStub()
    res = await a.fetch()
    raw = next(it for it in res.items if it["code"] == "001237")
    norm = a.normalize(raw)
    assert norm["payload"]["board"] == "SZSE main"
    assert norm["payload"]["offer_price"] == 53.26
    # source_event_id is stable across exchange-prefixed scoping so a
    # later CNINFO row for the same secCode collapses onto this event.
    sid = a.source_event_id(raw)
    assert sid.endswith(":ipo:001237")


def _eastmoney_df_mistagged_688() -> pd.DataFrame:
    """One row mimicking the real 688797 臻宝科技 bug: a 688-prefix
    (unambiguously SSE STAR) code tagged 深圳证券交易所 in the 交易所
    column. Eastmoney has been observed to flip this tag between runs."""
    return pd.DataFrame(
        [
            {
                "股票代码": "688797",
                "股票简称": "臻宝科技",
                "申购代码": "787797",
                "交易所": "深圳证券交易所",  # WRONG — 688 is SSE STAR
                "板块": "科创板",
                "申购日期": pd.Timestamp("2026-06-12"),
                "上市日期": pd.NaT,
                "发行价格": float("nan"),
                "首日收盘价": float("nan"),
                "发行市盈率": float("nan"),
                "行业市盈率": 67.7,
                "中签率": float("nan"),
            },
        ]
    )


def test_akshare_exchange_overrides_bad_eastmoney_tag() -> None:
    """Regression for the 688797 臻宝科技 double-alert: Eastmoney returned
    the SAME row twice in two sync runs with conflicting 交易所 tags
    (SZSE then SSE), producing two events with different source_event_ids
    (`szse:ipo:688797` and `sse:ipo:688797`) and two Telegram alerts 30
    minutes apart. The fix: derive exchange from the unambiguous
    6-digit code prefix (688 → SSE) and ignore the upstream tag."""
    items = normalize_rows(_eastmoney_df_mistagged_688())
    assert len(items) == 1
    row = items[0]
    # Inferred from 688 prefix, NOT from the row's 交易所 column.
    assert row["exchange"] == "SSE"
    assert row["board"] == "STAR"
    sid = AkshareIpoAdapter().source_event_id(row)
    assert sid == "sse:ipo:688797"


def test_akshare_exchange_from_code_covers_all_prefixes() -> None:
    """Every prefix _board_from_code recognizes must also yield an
    exchange — otherwise a mistagged Eastmoney row for that prefix
    silently falls back to the (possibly wrong) 交易所 column."""
    from catalyst_radar.adapters.akshare_ipo import (
        _board_from_code,
        _exchange_from_code,
    )

    for code in ("688000", "601000", "603000", "000001", "300001", "920218", "830000"):
        board = _board_from_code(code)
        exchange = _exchange_from_code(code)
        assert board is not None, f"missing board for {code}"
        assert exchange is not None, f"missing exchange for {code} (board={board})"


# ── adapter: CNINFO normalization (pure) ────────────────────────────


def test_cninfo_normalize_filters_and_classifies() -> None:
    from catalyst_radar.adapters.cninfo_filings import normalize_announcements

    sample = [
        # SSE STAR prospectus (688*) — kept, classified as 'prospectus'
        {
            "announcementId": "1225323943",
            "secCode": "688635",
            "secName": "长进光子",
            "announcementTitle": "长进光子首次公开发行股票并在科创板上市招股说明书",
            "announcementTime": 1779379200000,
            "adjunctUrl": "finalpage/2026-05-22/1225323943.PDF",
            "adjunctSize": 6181,
        },
        # SZSE prospectus_intent (招股意向书)
        {
            "announcementId": "1225323900",
            "secCode": "301669",
            "secName": "高特电子",
            "announcementTitle": "高特电子首次公开发行股票招股意向书",
            "announcementTime": 1779000000000,
            "adjunctUrl": "finalpage/2026-05-20/1225323900.PDF",
            "adjunctSize": 5800,
        },
        # BSE row (920218) — kept; classified as issue_result (发行结果公告).
        {
            "announcementId": "1225328039",
            "secCode": "920218",
            "secName": "新天力",
            "announcementTitle": (
                "新天力向不特定合格投资者公开发行股票"
                "并在北京证券交易所上市发行结果公告"
            ),
            "announcementTime": 1779379200000,
            "adjunctUrl": "finalpage/2026-05-22/1225328039.PDF",
            "adjunctSize": 376,
        },
        # Unrelated doc title — must be dropped (no classification match)
        {
            "announcementId": "9999",
            "secCode": "688999",
            "secName": "测试",
            "announcementTitle": "保荐机构关于发行人的辅导工作总结报告",
            "announcementTime": 1779000000000,
            "adjunctUrl": "finalpage/2026-05-22/9999.PDF",
            "adjunctSize": 100,
        },
    ]
    out = normalize_announcements(sample)
    by_code = {it["sec_code"]: it for it in out}
    # 688999 dropped (unrelated doc); three mainland boards kept.
    assert set(by_code) == {"688635", "301669", "920218"}
    assert by_code["688635"]["doc_type"] == "prospectus"
    assert by_code["688635"]["exchange"] == "SSE"
    assert by_code["688635"]["pdf_url"].startswith("http://static.cninfo.com.cn/")
    assert by_code["301669"]["doc_type"] == "prospectus_intent"
    assert by_code["301669"]["exchange"] == "SZSE"
    assert by_code["920218"]["doc_type"] == "issue_result"
    assert by_code["920218"]["exchange"] == "BSE"


# ── extractor ──────────────────────────────────────────────────────


class _FakePage:
    """Stand-in for pypdf PdfReader.pages[i] — only needs extract_text()."""

    def __init__(self, text: str) -> None:
        self._text = text

    def extract_text(self) -> str:
        return self._text


class _FakeReader:
    def __init__(self, page_texts: list[str]) -> None:
        self.pages = [_FakePage(t) for t in page_texts]


def _patch_reader(monkeypatch, page_texts: list[str]) -> None:
    """Make `pypdf.PdfReader` inside the extractor return our fake."""
    import catalyst_radar.services.cn_prospectus_extract as mod

    def fake_reader_cls(*_args, **_kwargs):
        return _FakeReader(page_texts)

    # The extractor imports PdfReader lazily inside the function, so
    # patching `pypdf.PdfReader` itself is the right hook.
    monkeypatch.setattr("pypdf.PdfReader", fake_reader_cls)
    return mod


def test_extract_business_section_finds_heading_and_stops_at_next_chapter(monkeypatch) -> None:
    pages = [
        "封面 招股说明书",  # cover
        "目录 ...... 第五节 业务与技术 ...... 80",  # TOC — has dot-leaders
        "第一节 释义",
        "第二节 概览",
        "第三节 风险因素",
        "第四节 发行概况",
        "第五节 业务与技术 主营业务介绍 公司是一家专门从事光纤研发与制造的企业",
        "继续业务介绍 主要客户包括华为、中兴等大型企业",
        "市场份额分析 公司在国内特种光纤市场占有率为25%",
        "第六节 财务会计信息",  # next chapter — extractor should stop here
        "财务报表细节",
    ]
    mod = _patch_reader(monkeypatch, pages)
    text, meta = mod.extract_business_section(b"fake-pdf-bytes", max_chars=100_000)
    assert meta["fallback"] is False
    assert meta["start_page"] == 7  # 1-indexed page where 第五节 starts
    assert meta["end_page"] == 10  # stopped at 第六节
    assert "主营业务介绍" in text
    assert "华为、中兴" in text
    assert "财务报表细节" not in text


def test_extract_business_section_fallback_when_no_heading(monkeypatch) -> None:
    pages = ["招股说明书"] + ["普通正文 page " + str(i) for i in range(40)]
    mod = _patch_reader(monkeypatch, pages)
    text, meta = mod.extract_business_section(b"fake-pdf-bytes")
    assert meta["fallback"] is True
    assert meta["pages_total"] == 41
    assert "招股说明书" in text


# ── sync service ────────────────────────────────────────────────────


async def test_sync_cn_ipos_creates_events_and_alerts(
    db_session: AsyncSession,
) -> None:
    from catalyst_radar.models.event import Event
    from catalyst_radar.models.notification import Notification
    from catalyst_radar.services.cn_ipo_sync import sync_cn_ipos

    s1 = await sync_cn_ipos(db_session, adapter=_AkshareStub())
    # All four mainland rows kept — SSE, SZSE, ChiNext, BSE.
    assert s1.fetched == 4
    assert s1.events_created == 4
    # All matched (CN enabled by default in eodhd_ipo_enabled_countries=US,HK,CN).
    assert s1.matched == 4

    e688 = (
        await db_session.execute(select(Event).where(Event.symbol == "688635"))
    ).scalar_one()
    assert e688.country == "CN"
    assert e688.exchange == "SSE"
    assert e688.source_event_id.endswith(":ipo:688635")

    # BSE row is now ingested too.
    bse = (
        await db_session.execute(select(Event).where(Event.symbol == "920218"))
    ).scalar_one()
    assert bse.country == "CN"
    assert bse.exchange == "BSE"

    # Idempotent on re-run.
    s2 = await sync_cn_ipos(db_session, adapter=_AkshareStub())
    assert s2.events_created == 0
    assert s2.notifications_created == 0
    notifs = (await db_session.execute(select(func.count()).select_from(Notification))).scalar_one()
    assert notifs == s1.notifications_created


async def test_sync_cn_ipos_disabled(db_session: AsyncSession) -> None:
    from catalyst_radar.repositories.config_repository import ConfigRepository
    from catalyst_radar.services.cn_ipo_sync import sync_cn_ipos

    await ConfigRepository(db_session).set("cn_ipo_source_akshare_enabled", False)
    await db_session.commit()
    out = await sync_cn_ipos(db_session, adapter=_AkshareStub())
    assert (out.fetched, out.events_created, out.matched) == (0, 0, 0)
