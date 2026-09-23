"""Tushare Pro `share_float` adapter — A-share share-unlock schedules.

`pro.share_float(ts_code=...)` returns every unlock event for a single
A-share ticker, past and future, broken down per holder. Schema:
    ts_code, ann_date, float_date, float_share, float_ratio,
    holder_name, share_type
Dates are YYYYMMDD strings. `float_share` is in shares; `float_ratio`
is the % of total share count unlocking on that date.

The adapter is per-ticker because the endpoint is per-ticker — there is
no bulk "all upcoming unlocks" query in the 120-credit tier. The sync
service iterates tracked CN companies.

**Rate limit at the 120-credit tier: 1 call/hour for share_float.** Calls
beyond that return a Chinese 频率超限 error which we log as a warning
and treat as an empty result (the ticker just gets re-attempted on the
next sync run). With N tracked CN tickers, full refresh of all rows
takes ~N hours of wall clock. Upgrading to the 2 000-credit tier
(~¥200 one-time donation) lifts the cap to 500/min.

tushare is a blocking client (requests under the hood); we offload to
a thread the same way akshare_ipo does.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pandas as pd

from catalyst_radar.config import settings
from catalyst_radar.logging import get_logger

log = get_logger(__name__)


def _to_ts_code(symbol: str, exchange: str) -> str | None:
    """Map our internal (symbol, exchange) to tushare's `ts_code` shape
    (`<ticker>.SH` for SSE, `<ticker>.SZ` for SZSE). Returns None for
    anything that isn't a 6-digit A-share ticker."""
    if not symbol or not exchange or len(symbol) != 6 or not symbol.isdigit():
        return None
    ex = exchange.upper()
    if ex == "SSE":
        return f"{symbol}.SH"
    if ex == "SZSE":
        return f"{symbol}.SZ"
    return None


def _parse_date(value: Any) -> date | None:
    """tushare ships dates as YYYYMMDD strings — sometimes ints, sometimes
    pandas NaN. Return a naive `date` (no tz, since these are exchange
    dates, not timestamps)."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    s = str(value).strip()
    if not s or s == "nan":
        return None
    try:
        return datetime.strptime(s[:8], "%Y%m%d").date()
    except (ValueError, TypeError):
        return None


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass(slots=True)
class UnlockRow:
    """One holder's unlock on one date. We keep the holder breakdown
    intact so the alert can render 'Holder X: 5%  Holder Y: 2%' and so
    the user can judge concentration risk."""

    ts_code: str
    ann_date: date | None
    float_date: date
    float_share: float | None
    float_ratio: float | None  # % of total shares
    holder_name: str
    share_type: str


@dataclass(slots=True)
class UpcomingUnlock:
    """All holders unlocking on the same `float_date` for one ticker,
    aggregated for the catalyst event. The sum is what the trader
    actually cares about (total supply hitting the float that day)."""

    ts_code: str
    symbol: str
    exchange: str  # SSE | SZSE
    float_date: date
    total_share: float
    total_ratio: float  # summed % across holders
    holders: list[UnlockRow]

    @property
    def primary_share_type(self) -> str:
        """Pick the most common share_type as the headline label — when
        the same date carries multiple types (rare), the dominant one
        tells the story."""
        if not self.holders:
            return ""
        from collections import Counter

        c = Counter(h.share_type for h in self.holders if h.share_type)
        return c.most_common(1)[0][0] if c else ""


class TushareConfigError(RuntimeError):
    """Raised when TUSHARE_API_TOKEN is not configured."""


class TushareUnlocksAdapter:
    """Per-ticker share_float fetcher.

    The tushare client is created lazily on first use because importing
    tushare imports requests + a whole tree of optional Asia financial
    packages — pointless at module import for processes that never need
    it (the API server, the dispatcher)."""

    source_name = "tushare_share_float"
    schema_name = "tushare.share_float.v1"

    def __init__(self, api_token: str | None = None) -> None:
        self.api_token = api_token if api_token is not None else settings.tushare_api_token
        self._client: Any = None

    @property
    def configured(self) -> bool:
        return bool(self.api_token)

    def _require_client(self) -> Any:
        if not self.api_token:
            raise TushareConfigError("TUSHARE_API_TOKEN is not configured")
        if self._client is None:
            import tushare as ts

            ts.set_token(self.api_token)
            self._client = ts.pro_api(timeout=settings.tushare_request_timeout_seconds)
        return self._client

    async def fetch_unlocks(self, *, symbol: str, exchange: str) -> list[UnlockRow]:
        """All unlock rows for one ticker, history + future. Caller is
        responsible for filtering to the look-ahead window."""
        ts_code = _to_ts_code(symbol, exchange)
        if ts_code is None:
            return []
        client = self._require_client()
        try:
            df = await asyncio.to_thread(self._fetch_blocking, client, ts_code)
        except Exception as exc:  # noqa: BLE001 - one ticker must not abort the run
            log.warning("tushare_share_float_failed", ts_code=ts_code, error=repr(exc))
            return []
        rows: list[UnlockRow] = []
        for _, r in df.iterrows():
            fd = _parse_date(r.get("float_date"))
            if fd is None:
                continue
            rows.append(
                UnlockRow(
                    ts_code=str(r.get("ts_code") or ts_code).strip(),
                    ann_date=_parse_date(r.get("ann_date")),
                    float_date=fd,
                    float_share=_as_float(r.get("float_share")),
                    float_ratio=_as_float(r.get("float_ratio")),
                    holder_name=str(r.get("holder_name") or "").strip(),
                    share_type=str(r.get("share_type") or "").strip(),
                )
            )
        return rows

    @staticmethod
    def _fetch_blocking(client: Any, ts_code: str) -> pd.DataFrame:
        return client.share_float(ts_code=ts_code)


def group_upcoming(
    rows: list[UnlockRow],
    *,
    symbol: str,
    exchange: str,
    today: date,
    lookahead_days: int,
) -> list[UpcomingUnlock]:
    """Pure: filter to rows in [today, today + lookahead_days], group by
    float_date, sum shares + ratios across holders. Output is sorted by
    float_date ascending so the caller alerts the earliest one first."""
    cutoff = today + timedelta(days=lookahead_days)
    in_window = [r for r in rows if today <= r.float_date <= cutoff]
    by_date: dict[date, list[UnlockRow]] = {}
    for r in in_window:
        by_date.setdefault(r.float_date, []).append(r)

    out: list[UpcomingUnlock] = []
    for fd, holders in sorted(by_date.items()):
        total_share = sum(h.float_share or 0 for h in holders)
        total_ratio = sum(h.float_ratio or 0 for h in holders)
        ts_code = holders[0].ts_code
        out.append(
            UpcomingUnlock(
                ts_code=ts_code,
                symbol=symbol,
                exchange=exchange,
                float_date=fd,
                total_share=total_share,
                total_ratio=total_ratio,
                holders=holders,
            )
        )
    return out


def importance_for_ratio(
    ratio_pct: float,
    *,
    high_threshold: float,
    medium_threshold: float,
) -> str:
    """Importance derived from float_ratio. > high → 'high'; > medium →
    'medium'; else 'low'. Tunable from the runtime config."""
    if ratio_pct >= high_threshold:
        return "high"
    if ratio_pct >= medium_threshold:
        return "medium"
    return "low"


def utcnow_date() -> date:
    return datetime.now(UTC).date()
