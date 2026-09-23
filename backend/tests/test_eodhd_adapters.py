"""Fixture-pinned tests for adapters/eodhd_calendar.py and adapters/eodhd_news.py.

``fetch`` is exercised against a fake ``httpx.AsyncClient`` returning canned
responses built from ``tests/fixtures/eodhd_*_adapter.json`` — no live EODHD
traffic. Fixture dates are inert at the adapter layer (no reminder-window
logic runs here; window-sensitive fixtures live in test_calendar_sync.py).
"""

import json
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from catalyst_radar.adapters.eodhd import EodhdConfigError
from catalyst_radar.adapters.eodhd_calendar import (
    EodhdEarningsAdapter,
    EodhdIpoAdapter,
    default_window,
)
from catalyst_radar.adapters.eodhd_news import EodhdNewsAdapter, to_news_code

FX = Path(__file__).parent / "fixtures"
EARNINGS = json.loads((FX / "eodhd_earnings_adapter.json").read_text())
IPOS = json.loads((FX / "eodhd_ipos_adapter.json").read_text())
NEWS = json.loads((FX / "eodhd_news_adapter.json").read_text())

WINDOW = (date(2026, 6, 1), date(2026, 6, 30))


class _FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        json_data: object = "__not_json__",
        text: str = "",
    ) -> None:
        self.status_code = status_code
        self._json = json_data
        self.text = text
        self.request = SimpleNamespace(url="https://eodhd.test/request")

    def json(self) -> object:
        if self._json == "__not_json__":
            raise ValueError("payload is not JSON")
        return self._json


def _fake_client(monkeypatch, responses: list[_FakeResponse]) -> list[dict]:
    """Replace httpx.AsyncClient with a canned-response stub; returns the
    recorded GET calls so tests can assert pagination behavior."""
    resps = list(responses)
    calls: list[dict] = []

    class _Client:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

        async def get(self, url: str, params: dict | None = None) -> _FakeResponse:
            calls.append({"url": url, "params": params})
            return resps.pop(0)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return calls


# =============================== earnings ====================================


async def test_earnings_fetch_happy_path_filters_malformed_row(monkeypatch) -> None:
    _fake_client(monkeypatch, [_FakeResponse(json_data=EARNINGS)])
    a = EodhdEarningsAdapter(api_key="test-key", base_url="https://eodhd.test/api")
    result = await a.fetch(WINDOW)
    assert result.http_status == 200
    assert result.schema_name == "eodhd.earnings.v1"
    assert result.payload == EARNINGS
    # 3 raw rows, one is a bare string -> only the 2 dict rows survive.
    assert len(result.items) == 2
    assert {r["code"] for r in result.items} == {"MU.US", "0700.HK"}


def test_earnings_normalize_every_field() -> None:
    a = EodhdEarningsAdapter(api_key="test-key")
    row = EARNINGS["earnings"][0]
    norm = a.normalize(row)
    assert norm["event_type"] == "earnings"
    assert norm["symbol"] == "MU"
    assert norm["exchange"] == "US"
    assert norm["company_name"] is None
    assert norm["title"] == "MU.US earnings"
    assert norm["event_date"] == datetime(2026, 6, 24, tzinfo=UTC)
    payload = norm["payload"]
    assert payload["code"] == "MU.US"
    assert payload["report_date"] == "2026-06-24"
    assert payload["fiscal_period_end"] == "2026-05-28"
    assert payload["before_after_market"] == "AfterMarket"
    assert payload["currency"] == "USD"
    assert payload["estimate"] == 1.45
    assert payload["actual"] is None
    assert payload["difference"] is None
    assert payload["percent"] is None


def test_earnings_normalize_tolerates_missing_consensus_fields() -> None:
    # The 0700.HK fixture row omits every optional consensus field.
    a = EodhdEarningsAdapter(api_key="test-key")
    norm = a.normalize(EARNINGS["earnings"][1])
    assert norm["symbol"] == "0700"
    assert norm["exchange"] == "HK"
    assert norm["event_date"] == datetime(2026, 6, 17, tzinfo=UTC)
    payload = norm["payload"]
    for key in ("before_after_market", "currency", "estimate", "actual", "difference", "percent"):
        assert payload[key] is None


def test_earnings_source_event_id_stable() -> None:
    a = EodhdEarningsAdapter(api_key="test-key")
    row = EARNINGS["earnings"][0]
    sid = a.source_event_id(row)
    assert sid == "eodhd:earnings:MU.US:2026-06-24:2026-05-28"
    assert a.source_event_id(dict(row)) == sid
    assert a.dedup_key(row) == a.dedup_key(dict(row))


async def test_earnings_fetch_zero_yield_variants(monkeypatch) -> None:
    # Non-JSON 200 body -> [] and the raw text kept as payload.
    _fake_client(monkeypatch, [_FakeResponse(text="<html>maintenance</html>")])
    a = EodhdEarningsAdapter(api_key="test-key")
    result = await a.fetch(WINDOW)
    assert result.items == []
    assert result.payload == "<html>maintenance</html>"

    # HTTP error -> [] (status recorded for the source_run, no raise).
    _fake_client(monkeypatch, [_FakeResponse(status_code=402, text="quota")])
    result = await a.fetch(WINDOW)
    assert result.items == []
    assert result.http_status == 402

    # 200 with an envelope missing the list key -> [].
    _fake_client(monkeypatch, [_FakeResponse(json_data={"type": "Earnings"})])
    result = await a.fetch(WINDOW)
    assert result.items == []

    # 200 with a non-dict JSON body -> [].
    _fake_client(monkeypatch, [_FakeResponse(json_data=["unexpected", "shape"])])
    result = await a.fetch(WINDOW)
    assert result.items == []


def test_earnings_fetch_requires_api_key() -> None:
    a = EodhdEarningsAdapter(api_key="")
    with pytest.raises(EodhdConfigError):
        a._require_key()


# ================================= IPOs ======================================


async def test_ipo_fetch_happy_path_filters_malformed_row(monkeypatch) -> None:
    _fake_client(monkeypatch, [_FakeResponse(json_data=IPOS)])
    a = EodhdIpoAdapter(api_key="test-key")
    result = await a.fetch(WINDOW)
    assert result.http_status == 200
    assert result.schema_name == "eodhd.ipos.v1"
    # 3 raw rows, one is the integer 42 -> only the 2 dict rows survive.
    assert len(result.items) == 2
    assert {r["name"] for r in result.items} == {"Example Robotics Ltd", "HK Biotech Holdings"}


def test_ipo_normalize_every_field() -> None:
    a = EodhdIpoAdapter(api_key="test-key")
    norm = a.normalize(IPOS["ipos"][0])
    assert norm["event_type"] == "ipo"
    assert norm["symbol"] is None  # code "N/A" must not become a symbol
    assert norm["exchange"] == "NASDAQ"
    assert norm["company_name"] == "Example Robotics Ltd"
    assert norm["title"] == "IPO: Example Robotics Ltd"
    assert norm["event_date"] == datetime(2026, 5, 22, tzinfo=UTC)
    payload = norm["payload"]
    assert payload["code"] == "N/A"
    assert payload["name"] == "Example Robotics Ltd"
    assert payload["exchange"] == "NASDAQ"
    assert payload["currency"] == "USD"
    assert payload["start_date"] == "2026-05-22"
    assert payload["filing_date"] == "2026-04-10"
    assert payload["amended_date"] == "2026-05-01"
    assert payload["price_from"] == 18.0
    assert payload["price_to"] == 21.0
    assert payload["offer_price"] is None
    assert payload["shares"] == 25000000
    assert payload["deal_type"] == "Expected"


def test_ipo_normalize_tolerates_missing_optional_pricing() -> None:
    a = EodhdIpoAdapter(api_key="test-key")
    norm = a.normalize(IPOS["ipos"][1])
    assert norm["symbol"] == "1234"
    payload = norm["payload"]
    for key in ("price_from", "price_to", "offer_price", "shares", "amended_date"):
        assert payload[key] is None


def test_ipo_source_event_id_falls_back_to_name_for_na_code() -> None:
    a = EodhdIpoAdapter(api_key="test-key")
    sid = a.source_event_id(IPOS["ipos"][0])
    assert sid == "eodhd:ipo:NASDAQ:Example Robotics Ltd:2026-05-22:Expected"
    # Rows with a real code key on the code, not the name.
    sid_hk = a.source_event_id(IPOS["ipos"][1])
    assert sid_hk == "eodhd:ipo:HKEX:1234:2026-05-28:Expected"


async def test_ipo_fetch_zero_yield_on_empty_envelope(monkeypatch) -> None:
    _fake_client(monkeypatch, [_FakeResponse(json_data={"ipos": []})])
    a = EodhdIpoAdapter(api_key="test-key")
    result = await a.fetch(WINDOW)
    assert result.items == []
    assert result.http_status == 200


def test_default_window_spans_lookahead_days() -> None:
    start, end = default_window(14)
    assert (end - start).days == 14
    assert start == datetime.now(UTC).date()


# ================================= news ======================================


def test_to_news_code_maps_us_listing_exchanges() -> None:
    assert to_news_code("MU", "NASDAQ") == "MU.US"
    assert to_news_code("F", "NYSE") == "F.US"
    assert to_news_code("0700", "HK") == "0700.HK"
    assert to_news_code("005930", "KO") == "005930.KO"
    assert to_news_code("ABC", "  nasdaq ") == "ABC.US"  # case/space tolerant
    assert to_news_code("XYZ", None) == "XYZ"
    assert to_news_code("XYZ", "") == "XYZ"


async def test_news_fetch_happy_path_filters_malformed_row(monkeypatch) -> None:
    calls = _fake_client(monkeypatch, [_FakeResponse(json_data=NEWS)])
    a = EodhdNewsAdapter(api_key="test-key", base_url="https://eodhd.test/api")
    result = await a.fetch("MU.US")
    assert result.http_status == 200
    assert result.schema_name == "eodhd.news.v1"
    # 4 raw rows, one is a bare string -> only the 3 dict rows survive.
    assert len(result.items) == 3
    assert calls[0]["params"]["s"] == "MU.US"


async def test_news_fetch_stops_when_page_not_full(monkeypatch) -> None:
    # max_pages=3 but the first page has < 50 items -> exactly one request.
    calls = _fake_client(monkeypatch, [_FakeResponse(json_data=NEWS)])
    a = EodhdNewsAdapter(api_key="test-key", max_pages=3)
    await a.fetch("MU.US")
    assert len(calls) == 1


def test_news_normalize_every_field() -> None:
    a = EodhdNewsAdapter(api_key="test-key")
    norm = a.normalize(NEWS[0])
    # title/content come back stripped of the fixture's padding whitespace.
    assert norm["title"] == "Micron unveils next-generation HBM4 memory for AI accelerators"
    assert norm["content"].startswith("Micron announced volume production")
    assert norm["content"] == norm["content"].strip()
    assert norm["link"] == "https://example.com/micron-hbm4"
    assert norm["date"] == "2026-05-16T09:15:00+00:00"
    assert norm["symbols"] == ["MU.US"]
    assert norm["tags"] == ["Technology"]
    assert norm["sentiment"] == {"polarity": 0.4, "neg": 0.02, "neu": 0.7, "pos": 0.28}


def test_news_normalize_missing_and_garbage_date() -> None:
    a = EodhdNewsAdapter(api_key="test-key")
    # Missing date -> None (downstream freshness gate treats it as undated).
    no_date = a.normalize(NEWS[1])
    assert no_date["date"] is None
    assert no_date["symbols"] == []
    assert no_date["tags"] == []
    assert no_date["sentiment"] == {}
    # Garbage date passes through verbatim — the adapter does not parse dates;
    # it must not raise on junk from the wire.
    garbage = a.normalize(NEWS[2])
    assert garbage["date"] == "not-a-date"
    # ids/dedup stay derivable for both rows without raising.
    assert a.source_event_id(NEWS[1]).startswith("eodhd:news:")
    assert a.source_event_id(NEWS[2]).startswith("eodhd:news:")
    assert a.dedup_key(no_date) and a.dedup_key(garbage)


def test_news_source_event_id_link_first_title_date_fallback() -> None:
    a = EodhdNewsAdapter(api_key="test-key")
    with_link_a = {"link": "https://example.com/x", "title": "t1", "date": "2026-05-16"}
    with_link_b = {"link": "https://example.com/x", "title": "t2", "date": "2026-05-17"}
    # Same link -> same id, regardless of title/date.
    assert a.source_event_id(with_link_a) == a.source_event_id(with_link_b)
    # No link -> title|date basis; different dates stay distinct.
    linkless_a = {"link": None, "title": "headline", "date": "2026-05-16"}
    linkless_b = {"link": None, "title": "headline", "date": "2026-05-17"}
    assert a.source_event_id(linkless_a) != a.source_event_id(linkless_b)
    assert a.dedup_key(with_link_a) == a.dedup_key(with_link_b)
    assert a.dedup_key(linkless_a) != a.dedup_key(linkless_b)


async def test_news_fetch_zero_yield_variants(monkeypatch) -> None:
    a = EodhdNewsAdapter(api_key="test-key")

    # HTTP error -> [] with the status recorded.
    _fake_client(monkeypatch, [_FakeResponse(status_code=500, text="boom")])
    result = await a.fetch("MU.US")
    assert result.items == []
    assert result.http_status == 500
    assert result.payload == "boom"

    # 200 with a non-list JSON body (error envelope) -> [].
    _fake_client(monkeypatch, [_FakeResponse(json_data={"error": "unknown symbol"})])
    result = await a.fetch("BOGUS.TW")
    assert result.items == []

    # 200 with a non-JSON body -> [].
    _fake_client(monkeypatch, [_FakeResponse(text="<html>edge cache</html>")])
    result = await a.fetch("MU.US")
    assert result.items == []

    # 200 with an empty list -> [] (legitimate no-news case).
    _fake_client(monkeypatch, [_FakeResponse(json_data=[])])
    result = await a.fetch("MU.US")
    assert result.items == []


def test_news_fetch_requires_api_key() -> None:
    a = EodhdNewsAdapter(api_key="")
    with pytest.raises(EodhdConfigError):
        a._require_key()
