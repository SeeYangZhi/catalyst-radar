"""Tests for the shared HK stock-code canonicalizer (adapters/hk_codes.py).

The AAStocks calendar, HKEXnews New Listing Report, and HKEXnews
prospectus adapters all key HK IPO rows on ``hkex:ipo:<code>``; the
listed-flip in ``hk_ipo_enrich`` matches AAStocks-minted symbols against
HKEX-feed codes. These tests pin the one shared implementation (first
contiguous digit run, zero-padded to 5) so the sides can never diverge
again, and cover every edge case the three per-adapter copies used to
pin separately.
"""

import pytest

from catalyst_radar.adapters.hk_codes import pad_code


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # plain codes pad to 5
        ("6871", "06871"),
        ("1234", "01234"),
        # already-padded codes pass through
        ("06082", "06082"),
        # whitespace tolerated
        (" 06871 ", "06871"),
        ("\t2618\n", "02618"),
        # footnoted cells: only the FIRST digit run counts (the bug —
        # all-digit concatenation yielded "26181" on the AAStocks side)
        ("2618 (Note 1)", "02618"),
        ("2618 (Notes 1 and 2)", "02618"),
        # >5-digit run keeps the last 5 (pre-existing truncation rule)
        ("123456", "23456"),
        # non-string inputs (xlsx cells arrive as ints)
        (2618, "02618"),
        (6082, "06082"),
        # no digits at all -> None
        ("", None),
        (None, None),
        ("no digits", None),
        ("no-digits", None),
    ],
)
def test_pad_code_edge_cases(raw: object, expected: str | None) -> None:
    assert pad_code(raw) == expected


def test_all_three_adapters_canonicalize_identically() -> None:
    """Regression for the flip miss: a footnote-contaminated code must
    produce the same hkex:ipo:<code> key on the AAStocks (discovery) and
    HKEXnews (newly-listed) sides, and the prospectus adapter's
    no-leading-zeros table key must derive from the same digit run."""
    from catalyst_radar.adapters.aastocks_ipo import AastocksIpoAdapter
    from catalyst_radar.adapters.hkex_newly_listed import HkexNewlyListedAdapter
    from catalyst_radar.adapters.hkex_prospectus import _norm_code

    contaminated = "2618 (Note 1)"
    aastocks_id = AastocksIpoAdapter().source_event_id({"code": contaminated})
    hkex_id = HkexNewlyListedAdapter().source_event_id({"code": contaminated})
    assert aastocks_id == hkex_id == "hkex:ipo:02618"
    assert _norm_code(contaminated) == "2618"
