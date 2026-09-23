"""Mainland China IPO calendar adapter (SSE + SZSE + BSE) via akshare.

akshare wraps Eastmoney's IPO-history endpoint (`stock_xgsglb_em`) which
returns ~3,600 A-share IPOs covering subscription / listing / pricing.
Free, no auth, no captcha. Covers all three mainland boards: SSE main +
STAR + SZSE main + ChiNext + BSE (北交所).

This is the "late-stage" trigger: rows appear here once the IPO is
priced (typically T-3 to T-1 days from listing). Earlier-stage
detection (受理 / 问询 / 上会 / 注册 events that fire weeks/months
before listing) comes from `akshare_ipo_review.py`.

akshare runs blocking I/O against Eastmoney through pandas; we offload
it to a thread so the asyncio loop is never blocked.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from catalyst_radar.adapters._retry import retry_sync
from catalyst_radar.adapters.base import FetchResult, SourceAdapter
from catalyst_radar.dedup import dedup_key as make_dedup_key
from catalyst_radar.dedup import source_event_id as make_source_event_id
from catalyst_radar.logging import get_logger

log = get_logger(__name__)


# Eastmoney's `交易所` column carries the human-readable Chinese name.
# We normalize to our internal exchange codes — SSE/SZSE/BSE all map to
# country CN via `ipo_sync.country_for_exchange`.
_EXCHANGE_MAP = {
    "上海证券交易所": "SSE",
    "深圳证券交易所": "SZSE",
    "北京证券交易所": "BSE",
}


def _norm_code(raw: Any) -> str | None:
    """A-share / BSE tickers are 6 digits; reject anything else."""
    s = "".join(ch for ch in str(raw or "") if ch.isdigit())
    return s.zfill(6)[-6:] if s else None


def _board_from_code(code: str | None) -> str | None:
    """Infer board from the 6-digit ticker prefix. Keeps the adapter
    honest about which exchange a row belongs to even when 板块 carries
    a less-specific label like '非科创板'."""
    if not code or len(code) != 6:
        return None
    first = code[0]
    first3 = code[:3]
    if first == "6":
        return "STAR" if first3 == "688" else "SSE main"
    if first == "0":
        return "SZSE main"
    if first == "3":
        return "ChiNext"
    if first in {"4", "8", "9"}:
        return "BSE"
    return None


def _exchange_from_code(code: str | None) -> str | None:
    """A-share + BSE listing-exchange is unambiguous from the 6-digit
    ticker prefix in the IPO calendar: 6→SSE (main + STAR), 0/3→SZSE
    (main + ChiNext), 4/8/9→BSE (historical NEEQ ranges + the new BSE
    92xxxx range; SSE B-shares at 9xxxxx don't list new IPOs so the
    9-prefix is unambiguous in this adapter's scope). Used to override
    Eastmoney's 交易所 column, which has been observed to flip between
    runs (688797 臻宝科技 was tagged 深圳证券交易所 in one fetch and
    上海证券交易所 in the next, producing two events with different
    sids for the same listing — same risk applies to mistagged BSE
    rows). Mirrors the same prefix scheme used by ``_board_from_code``."""
    if not code or len(code) != 6:
        return None
    first = code[0]
    if first == "6":
        return "SSE"
    if first in {"0", "3"}:
        return "SZSE"
    if first in {"4", "8", "9"}:
        return "BSE"
    return None


def _parse_date(value: Any) -> datetime | None:
    # pandas NaT / pd.NA / numpy.nan / None all coerce via `pd.isna`. The
    # scalar overload accepts singletons but raises ValueError on arrays;
    # we never pass arrays here so we don't have to guard against that.
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            ts = pd.to_datetime(value)
        except (ValueError, TypeError):
            return None
        if pd.isna(ts):
            return None
        dt = ts.to_pydatetime()
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _as_float(value: Any) -> float | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _row_to_item(row: pd.Series) -> dict[str, Any] | None:
    """Normalize one Eastmoney row. Returns None for malformed tickers
    or codes Eastmoney's prefix scheme doesn't recognize."""
    code = _norm_code(row.get("股票代码"))
    name = str(row.get("股票简称", "")).strip()
    if not code or not name:
        return None
    # Prefer the code-prefix-derived exchange (deterministic) over
    # Eastmoney's 交易所 column, which has been observed to flip between
    # runs (688797 was tagged SZSE on one fetch and SSE on the next,
    # producing two separate events + two Telegram alerts). Fall back to
    # the column when the prefix is something we don't recognize.
    exchange = _exchange_from_code(code) or _EXCHANGE_MAP.get(
        str(row.get("交易所", "")).strip()
    )
    if exchange is None:
        return None
    tagged = _EXCHANGE_MAP.get(str(row.get("交易所", "")).strip())
    if tagged is not None and tagged != exchange:
        log.warning(
            "akshare_ipo_exchange_tag_mismatch",
            code=code,
            name=name,
            tagged_exchange=tagged,
            inferred_exchange=exchange,
        )
    return {
        "code": code,
        "name": name,
        "exchange": exchange,
        "board": _board_from_code(code) or str(row.get("板块", "")).strip() or None,
        "subscription_date": row.get("申购日期"),
        "listing_date": row.get("上市日期"),
        "offer_price": _as_float(row.get("发行价格")),
        "first_day_close": _as_float(row.get("首日收盘价")),
        "issue_pe": _as_float(row.get("发行市盈率")),
        "industry_pe": _as_float(row.get("行业市盈率")),
        "win_rate": _as_float(row.get("中签率")),
    }


def normalize_rows(df: pd.DataFrame) -> list[dict[str, Any]]:
    """Pure: Eastmoney DataFrame -> list of normalized A-share IPO items.
    Drops 北交所 rows and any row missing a valid ticker."""
    items: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        item = _row_to_item(row)
        if item is not None:
            items.append(item)
    return items


class AkshareIpoAdapter(SourceAdapter):
    """Wraps `akshare.stock_xgsglb_em(symbol='全部股票')`."""

    source_name = "akshare_ipo"
    schema_name = "akshare.stock_xgsglb_em.v1"
    source_url = "https://data.eastmoney.com/xg/xg/"

    async def fetch(self, target: Any = None) -> FetchResult:
        try:
            df = await asyncio.to_thread(self._fetch_blocking)
        except Exception as exc:  # noqa: BLE001 - one bad sync must not abort the run
            log.warning("akshare_ipo_fetch_failed", error=repr(exc))
            return FetchResult(
                source_name=self.source_name,
                schema_name=self.schema_name,
                source_url=self.source_url,
                http_status=0,
                payload=f"fetch failed: {exc!r}",
                items=[],
            )
        items = normalize_rows(df)
        return FetchResult(
            source_name=self.source_name,
            schema_name=self.schema_name,
            source_url=self.source_url,
            http_status=200,
            payload={"row_count": int(len(df))},
            items=items,
        )

    @staticmethod
    def _fetch_blocking() -> pd.DataFrame:
        # Imported lazily so test fixtures can monkeypatch the symbol on
        # the module without paying the akshare import cost at startup.
        import akshare as ak

        # Same Eastmoney upstream as the review/news adapters — it truncates
        # responses mid-stream under load (ChunkedEncodingError). Retry with
        # the conservative 5s/20s backoff shared default; Eastmoney
        # rate-limits aggressive callers so we keep attempts low.
        return retry_sync(
            lambda: ak.stock_xgsglb_em(symbol="全部股票"),
            attempts=3,
            base_delay=5.0,
            max_delay=20.0,
            # akshare wraps `requests`, whose ConnectionError/Timeout aren't in
            # the httpx-oriented transient set; Eastmoney is flaky but always
            # recovers, so retry whatever it throws (the prior hand-rolled
            # loop's behaviour). The outer fail-soft still catches a real outage.
            retry_on=(Exception,),
            label="akshare_ipo",
        )

    def normalize(self, raw_item: dict[str, Any]) -> dict[str, Any]:
        code = raw_item["code"]
        exchange = raw_item["exchange"]
        # For A-shares the SUBSCRIPTION date (申购日期) is the actionable
        # date — that's when retail打新 happens; the listing date is a
        # later mechanical follow-on. Eastmoney often posts a row before
        # the listing date is finalized (listing_date NaN), and without
        # this fallback those upcoming-IPO rows never got an event_date,
        # so the alert-window logic silently dropped them.
        listing = _parse_date(raw_item.get("listing_date"))
        subscription = _parse_date(raw_item.get("subscription_date"))
        return {
            "event_type": "ipo",
            "symbol": code,
            "exchange": exchange,
            "company_name": raw_item["name"],
            "title": f"IPO: {raw_item['name']}",
            "event_date": listing or subscription,
            "payload": {
                "code": code,
                "name": raw_item["name"],
                "exchange": exchange,
                "board": raw_item.get("board"),
                "source": "akshare",
                "subscription_date": (
                    str(raw_item["subscription_date"])[:10]
                    if raw_item.get("subscription_date") is not None
                    and not pd.isna(raw_item.get("subscription_date"))
                    else None
                ),
                "listing_date": (
                    str(raw_item["listing_date"])[:10]
                    if raw_item.get("listing_date") is not None
                    and not pd.isna(raw_item.get("listing_date"))
                    else None
                ),
                "offer_price": raw_item.get("offer_price"),
                "first_day_close": raw_item.get("first_day_close"),
                "issue_pe": raw_item.get("issue_pe"),
                "industry_pe": raw_item.get("industry_pe"),
                "win_rate": raw_item.get("win_rate"),
            },
        }

    def source_event_id(self, raw_item: dict[str, Any]) -> str:
        # 6-digit ticker is the stable cross-source key — CNINFO filings
        # carry the same secCode, so the two sources dedupe onto one Event.
        return make_source_event_id(
            raw_item.get("exchange", "cn").lower(), "ipo", raw_item["code"]
        )

    def dedup_key(self, raw_or_normalized_item: dict[str, Any]) -> str:
        return make_dedup_key(self.source_event_id(raw_or_normalized_item))
