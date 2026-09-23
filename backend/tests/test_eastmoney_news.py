import pandas as pd

from catalyst_radar.adapters.eastmoney_news import (
    EastmoneyNewsAdapter,
    _to_iso_shanghai,
    normalize_rows,
    supports,
)


def _df(rows: list[dict]) -> pd.DataFrame:
    """Build a DataFrame with the exact columns ak.stock_news_em returns."""
    cols = ["关键词", "新闻标题", "新闻内容", "发布时间", "文章来源", "新闻链接"]
    return pd.DataFrame(rows, columns=cols)


def test_supports_only_cn_exchanges() -> None:
    assert supports("SSE") and supports("SZSE") and supports("BSE")
    assert supports(" sse ")  # case + whitespace tolerant
    assert not supports("US")
    assert not supports("HK")
    assert not supports("TW")
    assert not supports(None)


def test_to_iso_shanghai_attaches_offset() -> None:
    # Naive Shanghai wall-clock -> ISO with explicit +08:00 so the
    # downstream UTC-assume-on-naive parser doesn't skew the age by 8h.
    assert _to_iso_shanghai("2026-06-03 21:31:00") == "2026-06-03T21:31:00+08:00"


def test_to_iso_shanghai_handles_missing() -> None:
    assert _to_iso_shanghai(None) is None
    assert _to_iso_shanghai("") is None
    assert _to_iso_shanghai("   ") is None
    assert _to_iso_shanghai(pd.NaT) is None
    assert _to_iso_shanghai(float("nan")) is None
    assert _to_iso_shanghai("not a date") is None


def test_normalize_rows_maps_columns_and_dates() -> None:
    df = _df(
        [
            {
                "关键词": "688797",
                "新闻标题": "臻宝科技披露上市发行时间表：6月9日询价",
                "新闻内容": "公告称，公司此次发行的网上申购代码为787797。",
                "发布时间": "2026-06-03 21:31:00",
                "文章来源": "证券时报网",
                "新闻链接": "http://finance.eastmoney.com/a/202606033759314219.html",
            }
        ]
    )
    items = normalize_rows(df)
    assert len(items) == 1
    it = items[0]
    assert it["title"].startswith("臻宝科技")
    assert it["content"].startswith("公告称")
    assert it["link"] == "http://finance.eastmoney.com/a/202606033759314219.html"
    assert it["date"] == "2026-06-03T21:31:00+08:00"
    assert it["source_label"] == "证券时报网"


def test_normalize_rows_drops_titleless_and_blank_links() -> None:
    df = _df(
        [
            {  # no title → dropped
                "关键词": "688797",
                "新闻标题": "  ",
                "新闻内容": "body",
                "发布时间": "2026-06-03 10:00:00",
                "文章来源": "src",
                "新闻链接": "http://x/1",
            },
            {  # blank link → link becomes None, item kept
                "关键词": "688797",
                "新闻标题": "real title",
                "新闻内容": "body",
                "发布时间": "2026-06-03 11:00:00",
                "文章来源": "src",
                "新闻链接": "",
            },
        ]
    )
    items = normalize_rows(df)
    assert len(items) == 1
    assert items[0]["title"] == "real title"
    assert items[0]["link"] is None


def test_source_event_id_collapses_mobile_desktop_variants() -> None:
    a = EastmoneyNewsAdapter()
    mobile = {"title": "t", "date": "2026-06-03T10:00:00+08:00", "link": "https://x.cn/mobile/a/"}
    desktop = {"title": "t", "date": "2026-06-03T10:00:00+08:00", "link": "https://x.cn/a/"}
    assert a.source_event_id(mobile) == a.source_event_id(desktop)
    assert a.dedup_key(mobile) == a.dedup_key(desktop)


def test_source_event_id_falls_back_to_title_date_without_link() -> None:
    a = EastmoneyNewsAdapter()
    one = {"title": "headline", "date": "2026-06-03T10:00:00+08:00", "link": None}
    two = {"title": "headline", "date": "2026-06-04T10:00:00+08:00", "link": None}
    # Different dates → different sids (don't collapse distinct link-less stories).
    assert a.source_event_id(one) != a.source_event_id(two)
