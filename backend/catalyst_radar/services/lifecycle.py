"""Lifecycle-stage helpers for tracked companies.

A tracked company can be either *listed* (has a real exchange ticker —
the common case: AAPL.US, 0700.HK, 600519.SSE) or *pre-IPO* (no ticker
yet — identified by a CSRC reservation code like A25310 for CXMT or
A26029 for Unitree, assigned at filing acceptance and used through the
review process until the actual 6-digit listing ticker arrives).

The two lifecycles need different pipelines: ticker-keyed sources
(EODHD news, EODHD earnings, EDGAR enrichment) 404 on reservation
codes, while LLM-driven sources (web_search news, source discovery,
relationship preflight) work fine because they key on the company
name. This module is the single place that decides which lifecycle a
row is in, so the routing decision stays consistent across sync
services and frontend rendering.
"""

from __future__ import annotations

_PRE_IPO_CN_EXCHANGES = frozenset({"SSE", "SZSE", "BSE"})


def is_pre_ipo_cn_symbol(symbol: str | None, exchange: str | None) -> bool:
    """True when ``(symbol, exchange)`` looks like a CSRC pre-IPO
    reservation code (A-prefix followed by digits) targeted at a
    mainland-China board. False for everything else — including real
    A-share tickers (six-digit numeric: 600519, 688688, 000001) and
    US tickers that happen to start with 'A' (AAPL, A)."""
    if not symbol or not exchange:
        return False
    if exchange.strip().upper() not in _PRE_IPO_CN_EXCHANGES:
        return False
    s = symbol.strip().upper()
    return len(s) >= 2 and s[0] == "A" and s[1:].isdigit()
