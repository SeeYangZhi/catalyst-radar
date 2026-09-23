"""Tests for the pre-IPO lifecycle detector."""

from __future__ import annotations

import pytest

from catalyst_radar.services.lifecycle import is_pre_ipo_cn_symbol


@pytest.mark.parametrize(
    "symbol, exchange",
    [
        ("A25310", "SSE"),  # CXMT
        ("A26029", "SSE"),  # Unitree
        ("A20654", "SZSE"),
        ("A25076", "BSE"),
        ("a25310", "sse"),  # case-insensitive
        ("  A25310  ", "  SSE  "),  # padded
    ],
)
def test_recognises_csrc_reservation_codes(symbol: str, exchange: str) -> None:
    assert is_pre_ipo_cn_symbol(symbol, exchange) is True


@pytest.mark.parametrize(
    "symbol, exchange",
    [
        ("600519", "SSE"),  # real SSE main-board ticker (Kweichow Moutai)
        ("688635", "SSE"),  # real STAR ticker
        ("000001", "SZSE"),  # real SZSE main ticker
        ("AAPL", "US"),  # US ticker that starts with A
        ("A", "US"),  # Agilent — A + US (not CN)
        ("A25310", "US"),  # A-prefix but not a CN exchange
        ("A25310", "NASDAQ"),  # ditto
        ("A25310", ""),  # missing exchange
        ("", "SSE"),  # missing symbol
        (None, "SSE"),
        ("A25310", None),
        ("ABCDE", "SSE"),  # alpha tail, not digits
        ("A12B34", "SSE"),  # mixed alphanumeric
    ],
)
def test_rejects_everything_else(symbol, exchange) -> None:
    assert is_pre_ipo_cn_symbol(symbol, exchange) is False
