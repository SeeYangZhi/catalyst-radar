"""Fixture-pinned tests for adapters/aastocks_ipo.py (parser + normalization).

``parse_ipocalendar`` is a pure function over Playwright-rendered HTML, so it
is pinned offline against a minimal crafted calendar table that mirrors the
``<td data-symbol=...>`` cell structure AAStocks hydrates. The fixture dates
are inert (no reminder-window logic runs at the adapter layer).
"""

from datetime import UTC, datetime
from pathlib import Path

from catalyst_radar.adapters.aastocks_ipo import (
    AastocksIpoAdapter,
    _parse_date,
    parse_ipocalendar,
)

FIXTURE = (Path(__file__).parent / "fixtures" / "aastocks_ipocalendar_min.html").read_text()


# --- happy path -------------------------------------------------------------


def test_parse_ipocalendar_happy_path_every_field() -> None:
    rows = parse_ipocalendar(FIXTURE)
    by_code = {r["code"]: r for r in rows}
    row = by_code["09871"]
    assert row["code"] == "09871"  # "9871" zero-padded to 5 digits
    assert row["name"] == "Kestrel Robotics"
    assert row["list_date"] == "2026/05/18"
    assert row["app_open"] == "2026/05/08"
    assert row["app_close"] == "2026/05/13"
    assert row["ann_date"] == "2026/05/15"
    assert row["list_label"] == "Listing"  # stripped


def test_parse_ipocalendar_richest_row_wins_per_code() -> None:
    # The fixture repeats 09871 in a cell WITHOUT a list date; the row that
    # carries the listing date must win and codes stay unique.
    rows = parse_ipocalendar(FIXTURE)
    codes = [r["code"] for r in rows]
    assert codes.count("09871") == 1
    by_code = {r["code"]: r for r in rows}
    assert by_code["09871"]["list_date"] == "2026/05/18"


def test_parse_ipocalendar_optional_fields_absent_are_none() -> None:
    by_code = {r["code"]: r for r in parse_ipocalendar(FIXTURE)}
    row = by_code["09511"]
    assert row["name"] == "Harbourline Mobility"
    assert row["list_date"] is None
    assert row["ann_date"] is None
    assert row["list_label"] is None
    assert row["app_open"] == "2026/05/20"
    assert row["app_close"] == "2026/05/26"


# --- malformed rows fail soft -----------------------------------------------


def test_malformed_rows_skipped_good_rows_kept() -> None:
    rows = parse_ipocalendar(FIXTURE)
    by_code = {r["code"]: r for r in rows}
    # Nameless cell (data-desp="") and symbol-less cell are both dropped...
    assert "09999" not in by_code
    assert all(r["name"] != "Ghost Corp" for r in rows)
    # ...while the two good rows survive.
    assert set(by_code) == {"09871", "09511"}


# --- zero-yield is [] not an exception ---------------------------------------


def test_zero_yield_empty_and_garbage_inputs() -> None:
    assert parse_ipocalendar("") == []
    assert parse_ipocalendar(None) == []  # type: ignore[arg-type]
    assert parse_ipocalendar("<html><body>maintenance page</body></html>") == []
    assert parse_ipocalendar("not html at all <<<>>>") == []


# --- normalization + stable ids ----------------------------------------------


def test_normalize_maps_every_field() -> None:
    a = AastocksIpoAdapter()
    raw = next(r for r in parse_ipocalendar(FIXTURE) if r["code"] == "09871")
    norm = a.normalize(raw)
    assert norm["event_type"] == "ipo"
    assert norm["symbol"] == "09871"
    assert norm["exchange"] == "HKSE"
    assert norm["company_name"] == "Kestrel Robotics"
    assert norm["title"] == "IPO: Kestrel Robotics"
    assert norm["event_date"] == datetime(2026, 5, 18, tzinfo=UTC)
    payload = norm["payload"]
    assert payload["code"] == "09871"
    assert payload["name"] == "Kestrel Robotics"
    assert payload["exchange"] == "HKSE"
    assert payload["source"] == "aastocks"
    assert payload["list_date"] == "2026/05/18"
    assert payload["app_open"] == "2026/05/08"
    assert payload["app_close"] == "2026/05/13"
    assert payload["ann_date"] == "2026/05/15"
    assert payload["list_label"] == "Listing"


def test_normalize_tolerates_missing_list_date() -> None:
    a = AastocksIpoAdapter()
    norm = a.normalize({"code": "9511", "name": "Harbourline Mobility", "list_date": None})
    assert norm["event_date"] is None
    assert norm["symbol"] == "09511"


def test_source_event_id_zero_pad_equivalence() -> None:
    a = AastocksIpoAdapter()
    sid = a.source_event_id({"code": "9871", "name": "Kestrel Robotics"})
    assert sid == "hkex:ipo:09871"
    assert a.source_event_id({"code": "09871"}) == sid
    assert a.dedup_key({"code": "9871"}) == a.dedup_key({"code": "09871"})


# --- helpers ------------------------------------------------------------------


def test_parse_date_edge_cases() -> None:
    assert _parse_date("2026/05/18") == datetime(2026, 5, 18, tzinfo=UTC)
    assert _parse_date(None) is None
    assert _parse_date("") is None
    assert _parse_date("18-05-2026") is None  # wrong format -> None, no raise
