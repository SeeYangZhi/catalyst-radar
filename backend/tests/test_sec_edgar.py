"""Fixture-pinned tests for adapters/sec_edgar.py.

The extractors (``_visible_text`` / ``extract_summary`` / ``extract_497_summary``)
are pure functions; the resolver methods (``search_cik`` / ``latest_filing`` /
``fetch_summary``) are pinned by monkeypatching the adapter's ``_get`` — no
live EDGAR traffic. Real-shape fixtures: ``sec_edgar_fts.json`` /
``sec_edgar_submissions.json`` / ``sec_s1_summary.html`` (shared with
test_edgar_enrich) plus ``sec_edgar_submissions_malformed.json`` whose
best-ranked row is missing its primary document.
"""

import json
from pathlib import Path

from catalyst_radar.adapters.sec_edgar import (
    SecEdgarAdapter,
    _visible_text,
    extract_497_summary,
    extract_summary,
)

FX = Path(__file__).parent / "fixtures"
S1_HTML = (FX / "sec_s1_summary.html").read_text()
FTS = json.loads((FX / "sec_edgar_fts.json").read_text())
SUB = json.loads((FX / "sec_edgar_submissions.json").read_text())
SUB_MALFORMED = json.loads((FX / "sec_edgar_submissions_malformed.json").read_text())


class _Resp:
    def __init__(self, *, js: object = None, text: str = "") -> None:
        self._js = js
        self.text = text
        self.status_code = 200

    def json(self) -> object:
        if self._js is None:
            raise ValueError("not json")
        return self._js


def _patched(monkeypatch, route) -> SecEdgarAdapter:
    """Adapter whose _get is replaced by ``route(url) -> _Resp | None``."""
    a = SecEdgarAdapter()

    async def fake_get(url: str, *, params=None):
        return route(url)

    monkeypatch.setattr(a, "_get", fake_get)
    return a


# --- pure extractors: happy path ---------------------------------------------


def test_visible_text_strips_script_style_tags_and_entities() -> None:
    raw = (
        "<html><head><style>.x{color:red}</style>"
        "<script>var s = '<td>evil</td>';</script></head>"
        "<body><p>Our&nbsp;company &amp; subsidiaries</p></body></html>"
    )
    text = _visible_text(raw)
    assert "evil" not in text
    assert "color" not in text
    assert "<" not in text
    assert "company & subsidiaries" in text


def test_extract_summary_anchors_on_prospectus_summary() -> None:
    raw = (
        "<html><body><p>Table of contents and boilerplate to skip.</p>"
        "<h1>Prospectus Summary</h1>"
        "<p>We design industrial robots for warehouses.</p></body></html>"
    )
    out = extract_summary(raw)
    assert out.startswith("Prospectus Summary")
    assert "industrial robots" in out
    assert "boilerplate to skip" not in out


def test_extract_summary_real_filing_is_bounded() -> None:
    out = extract_summary(S1_HTML, max_chars=5000)
    assert 0 < len(out) <= 5000
    assert "<" not in out


def test_extract_summary_falls_back_to_document_head_without_anchor() -> None:
    out = extract_summary("<p>No anchor heading here, just body text.</p>")
    assert out.startswith("No anchor heading here")


def test_extract_497_summary_named_fund() -> None:
    raw = (
        '<p>XYZ Small-Mid Growth ETF (the "Fund") seeks to '
        "achieve long-term capital appreciation</p>"
    )
    out = extract_497_summary(raw)
    assert out == "XYZ Small-Mid Growth ETF seeks to achieve long-term capital appreciation."


def test_extract_497_summary_generic_fund_fallback() -> None:
    out = extract_497_summary("<p>The Fund seeks to track the performance of an index</p>")
    assert out == "The Fund seeks to track the performance of an index."


# --- pure extractors: zero-yield is empty string, never an exception ----------


def test_extractors_zero_yield_on_empty_and_garbage() -> None:
    assert extract_summary("") == ""
    assert extract_497_summary("") == ""
    assert extract_497_summary("<p>nothing fund-shaped here</p>") == ""
    # Garbage / truncated markup must not raise.
    assert isinstance(extract_summary("<<<>>> <td no-close"), str)
    assert isinstance(extract_497_summary("<<<>>>"), str)


# --- search_cik ----------------------------------------------------------------


async def test_search_cik_happy_path(monkeypatch) -> None:
    a = _patched(monkeypatch, lambda url: _Resp(js=FTS))
    assert await a.search_cik("BW Industrial Holdings Inc.") == "2080841"


async def test_search_cik_skips_malformed_hit_keeps_good(monkeypatch) -> None:
    fts = {
        "hits": {
            "hits": [
                # malformed: matching name but empty ciks list -> skipped
                {"_source": {"ciks": [], "display_names": ["Acme Robotics Inc. (CIK ?)"]}},
                # malformed: no _source payload at all -> skipped
                {},
                # good row
                {
                    "_source": {
                        "ciks": ["0001234567"],
                        "display_names": ["Acme Robotics Inc.  (CIK 0001234567)"],
                    }
                },
            ]
        }
    }
    a = _patched(monkeypatch, lambda url: _Resp(js=fts))
    assert await a.search_cik("Acme Robotics Inc.") == "1234567"


async def test_search_cik_zero_yield(monkeypatch) -> None:
    # transport failure -> None
    a = _patched(monkeypatch, lambda url: None)
    assert await a.search_cik("Acme Robotics Inc.") is None
    # non-JSON body -> None
    a = _patched(monkeypatch, lambda url: _Resp(text="<html>edgar down</html>"))
    assert await a.search_cik("Acme Robotics Inc.") is None
    # no display-name match -> None
    a = _patched(monkeypatch, lambda url: _Resp(js=FTS))
    assert await a.search_cik("Totally Unrelated Megacorp") is None


# --- latest_filing ---------------------------------------------------------------


async def test_latest_filing_prefers_highest_ranked_form(monkeypatch) -> None:
    a = _patched(monkeypatch, lambda url: _Resp(js=SUB))
    filing = await a.latest_filing("2080841")
    assert filing is not None
    assert filing["form"] == "S-1/A"  # outranks the plain S-1; FWP/DRS ignored
    assert filing["filing_date"] == "2026-03-17"
    assert filing["doc_url"] == (
        "https://www.sec.gov/Archives/edgar/data/2080841/000121390026028518/ea0252378-07.htm"
    )


async def test_latest_filing_skips_row_missing_primary_document(monkeypatch) -> None:
    # Fixture's best-ranked row (424B4) has primaryDocument "" -> must be
    # skipped; the S-1 row with a real document wins.
    a = _patched(monkeypatch, lambda url: _Resp(js=SUB_MALFORMED))
    filing = await a.latest_filing("2080841")
    assert filing is not None
    assert filing["form"] == "S-1"
    assert filing["doc_url"].endswith("/000121390025126775/ea0252378-04.htm")


async def test_latest_filing_zero_yield(monkeypatch) -> None:
    a = _patched(monkeypatch, lambda url: None)
    assert await a.latest_filing("2080841") is None
    a = _patched(monkeypatch, lambda url: _Resp(text="oops"))
    assert await a.latest_filing("2080841") is None
    # No registration-form rows at all -> None
    only_fwp = {
        "filings": {
            "recent": {
                "accessionNumber": ["0001-26-1"],
                "filingDate": ["2026-01-01"],
                "form": ["FWP"],
                "primaryDocument": ["x.htm"],
            }
        }
    }
    a = _patched(monkeypatch, lambda url: _Resp(js=only_fwp))
    assert await a.latest_filing("2080841") is None


# --- fetch_summary (end to end over stubbed _get) --------------------------------


def _route_full(url: str) -> _Resp:
    if "search-index" in url:
        return _Resp(js=FTS)
    if "data.sec.gov/submissions" in url:
        return _Resp(js=SUB)
    return _Resp(text=S1_HTML)


async def test_fetch_summary_happy_path(monkeypatch) -> None:
    a = _patched(monkeypatch, _route_full)
    out = await a.fetch_summary("BW Industrial Holdings Inc.")
    assert out is not None
    assert out["cik"] == "2080841"
    assert out["form"] == "S-1/A"
    assert out["filing_date"] == "2026-03-17"
    assert out["filing_url"].endswith("ea0252378-07.htm")
    assert len(out["summary"]) > 100


async def test_fetch_summary_uses_497_extractor_for_497(monkeypatch) -> None:
    sub_497 = {
        "filings": {
            "recent": {
                "accessionNumber": ["0001-26-2"],
                "filingDate": ["2026-02-02"],
                "form": ["497"],
                "primaryDocument": ["fund497.htm"],
            }
        }
    }

    def route(url: str) -> _Resp:
        if "search-index" in url:
            return _Resp(js=FTS)
        if "data.sec.gov/submissions" in url:
            return _Resp(js=sub_497)
        return _Resp(text='<p>BW Growth ETF (the "Fund") seeks to deliver growth</p>')

    a = _patched(monkeypatch, route)
    out = await a.fetch_summary("BW Industrial Holdings Inc.")
    assert out is not None
    assert out["form"] == "497"
    assert out["summary"] == "BW Growth ETF seeks to deliver growth."


async def test_fetch_summary_zero_yield_when_unresolvable(monkeypatch) -> None:
    a = _patched(monkeypatch, lambda url: None)
    assert await a.fetch_summary("BW Industrial Holdings Inc.") is None

    # CIK resolves but the filing document is empty -> None (not a crash).
    def route(url: str) -> _Resp:
        if "search-index" in url:
            return _Resp(js=FTS)
        if "data.sec.gov/submissions" in url:
            return _Resp(js=SUB)
        return _Resp(text="")

    a = _patched(monkeypatch, route)
    assert await a.fetch_summary("BW Industrial Holdings Inc.") is None
