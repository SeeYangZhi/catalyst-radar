"""CSRC IPO review-stage adapter (上会 / 上会通过 / 上会未通过).

`akshare.stock_ipo_review_em()` wraps Eastmoney's
`stock_ipo_review_em` endpoint — the IPO Review Committee
(发审委 / 上市委) hearing calendar for SSE main / STAR /
SZSE main / ChiNext / BSE. Each row is one company × one
hearing, identified by a CSRC reservation code like
``A25310`` (CXMT) or ``A26029`` (Unitree). These are *not*
the final 6-digit listing tickers — they're assigned at
filing and stay stable through the review process.

This is the **early-stage** trigger that complements
``akshare_ipo`` (which only sees rows once pricing is set,
T-3 to T-1 from listing): a review approval is the single
biggest IPO-lifecycle event for a pre-listing Chinese
company — confirmed by the user's headline examples.

The endpoint is slow (~80 s, pulls 5k+ historical rows
across 11 paginated requests) and runs in a thread so it
never blocks the loop. Cadence is bounded by its own beat
slot (default 6 h) — review committee meetings happen on
business days, daily-ish frequency.

Rows older than ``LOOKBACK_DAYS`` are dropped at the
adapter level so first-run alert flood and ongoing event
churn stay bounded; the alert filter further narrows to
``today - 14d ≤ 上会日期 ≤ today + 90d``.
"""

from __future__ import annotations

import asyncio
import urllib.parse
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd

from catalyst_radar.adapters._retry import retry_sync
from catalyst_radar.adapters.base import FetchResult, SourceAdapter
from catalyst_radar.dedup import dedup_key as make_dedup_key
from catalyst_radar.dedup import source_event_id as make_source_event_id
from catalyst_radar.logging import get_logger

log = get_logger(__name__)


# Map Eastmoney's 上市板块 to internal exchange codes. We surface BSE
# rows here (the user might want to filter at the alert layer rather
# than drop blindly) but exclude them from the alert path in the sync
# layer — same as akshare_ipo treats 北交所.
_BOARD_TO_EXCHANGE = {
    "上交所主板": "SSE",
    "上交所科创板": "SSE",
    "上交所风险警示板": "SSE",
    "深交所主板": "SZSE",
    "深交所创业板": "SZSE",
    "深交所风险警示板": "SZSE",
    "北交所": "BSE",
}

# Status normalization. The endpoint reports five distinct values; the
# English tag is what the formatter renders and what dedup is keyed on.
# A row reappearing with a new status creates a new event (different
# ``source_event_id``) and re-alerts — that's the desired behaviour
# for status transitions like 未上会 → 上会通过.
_STATUS_MAP = {
    "上会通过": "approved",
    "上会未通过": "rejected",
    "未上会": "scheduled",
    "暂缓表决": "postponed",
    "取消审核": "cancelled",
}

# Don't ingest rows whose hearing date is older than this. The endpoint
# returns rows going back many years; the historical tail bloats the
# events table and is never alert-actionable. The alert window is even
# narrower (set in the sync layer).
LOOKBACK_DAYS = 90

# Where the raw review rows come from — the shared CSRC committee
# calendar. Correct as *provenance* for the raw item, but useless as a
# per-event link: every company resolves to the same page.
REVIEW_CALENDAR_URL = "https://data.eastmoney.com/xg/cfg/"


def eastmoney_search_url(keyword: str) -> str:
    """Per-company Eastmoney link for a review-stage event.

    ``stock_ipo_review_em`` carries no per-company URL — only the shared
    committee calendar (`REVIEW_CALENDAR_URL`). The reservation code
    (``A26029``) isn't a real ticker yet, so there's no quote page to point
    at either. A search on the company's name is the most useful per-event
    link we can build from the row: it resolves to that firm's Eastmoney
    hub (quote once listed, news, filings). Falls back to the calendar
    landing page when the name is blank."""
    keyword = (keyword or "").strip()
    if not keyword:
        return REVIEW_CALENDAR_URL
    return "https://so.eastmoney.com/web/s?keyword=" + urllib.parse.quote(keyword)


def _parse_date(value: Any) -> datetime | None:
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


def _as_int(value: Any) -> int | None:
    f = _as_float(value)
    return int(f) if f is not None else None


def _row_to_item(row: pd.Series) -> dict[str, Any] | None:
    """Normalize one Eastmoney review row. Returns None for rows we
    cannot key (no reservation code) or that fall outside the lookback
    window."""
    code = str(row.get("股票代码", "")).strip()
    company_name = str(row.get("企业名称", "")).strip()
    short_name = str(row.get("股票简称", "")).strip() or company_name
    if not code or not company_name:
        return None

    board = str(row.get("上市板块", "")).strip()
    exchange = _BOARD_TO_EXCHANGE.get(board)
    status_cn = str(row.get("审核状态", "")).strip()
    status = _STATUS_MAP.get(status_cn)
    if status is None:
        return None

    meeting_date = _parse_date(row.get("上会日期"))
    if meeting_date is None:
        return None
    cutoff = datetime.now(UTC) - timedelta(days=LOOKBACK_DAYS)
    if meeting_date < cutoff:
        return None

    return {
        "code": code,
        "company_name": company_name,
        "short_name": short_name,
        "board": board or None,
        "exchange": exchange,
        "status": status,
        "status_cn": status_cn,
        "meeting_date": meeting_date,
        "announce_date": _parse_date(row.get("公告日期")),
        "listing_date": _parse_date(row.get("上市日期")),
        "underwriter": str(row.get("主承销商", "")).strip() or None,
        "share_count": _as_int(row.get("发行数量(股)")),
        "deal_size_cny": _as_float(row.get("拟融资额(元)")),
    }


def normalize_rows(df: pd.DataFrame) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        item = _row_to_item(row)
        if item is not None:
            items.append(item)
    return items


class AkshareIpoReviewAdapter(SourceAdapter):
    """Wraps ``akshare.stock_ipo_review_em()``."""

    source_name = "akshare_ipo_review"
    schema_name = "akshare.stock_ipo_review_em.v1"
    source_url = REVIEW_CALENDAR_URL

    async def fetch(self, target: Any = None) -> FetchResult:
        try:
            df = await asyncio.to_thread(self._fetch_blocking)
        except Exception as exc:  # noqa: BLE001 - never abort the beat run
            log.warning("akshare_ipo_review_fetch_failed", error=repr(exc))
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
            payload={"row_count": int(len(df)), "kept": len(items)},
            items=items,
        )

    # Eastmoney's response stream occasionally truncates mid-pagination
    # (~5/11 pages in) under back-to-back load and surfaces as
    # ChunkedEncodingError. The blip can persist across a few back-to-back
    # attempts (observed in prod: all 3 retries of one tick failed within ~3
    # min, recording an `empty` run that tripped the stale watchdog). 5
    # attempts with 10s→60s backoff stretches the retries across ~10 min
    # (each fetch itself is ~80s), so a multi-minute truncation window is
    # ridden out — while the generous, capped backoff stays polite (Eastmoney
    # rate-limits aggressive callers, so short/aggressive retries make it
    # worse). A genuine outage still fail-softs via the outer handler.
    @classmethod
    def _fetch_blocking(cls) -> pd.DataFrame:
        import akshare as ak

        return retry_sync(
            ak.stock_ipo_review_em,
            attempts=5,
            base_delay=10.0,
            max_delay=60.0,
            # akshare wraps `requests` — retry whatever Eastmoney throws (the
            # prior hand-rolled loop's behaviour), not just the httpx-oriented
            # transient set. Outer fail-soft still catches a real outage.
            retry_on=(Exception,),
            label="akshare_ipo_review",
        )

    def normalize(self, raw_item: dict[str, Any]) -> dict[str, Any]:
        return {
            "event_type": "ipo",
            "symbol": raw_item["code"],
            "exchange": raw_item.get("exchange"),
            "company_name": raw_item.get("short_name") or raw_item["company_name"],
            "title": (
                f"IPO review ({_STATUS_LABEL[raw_item['status']]}): "
                f"{raw_item.get('short_name') or raw_item['company_name']}"
            ),
            "event_date": raw_item["meeting_date"],
            "payload": {
                "stage": "review",
                "source": "akshare_ipo_review",
                "code": raw_item["code"],
                "company_name": raw_item["company_name"],
                "short_name": raw_item.get("short_name"),
                "exchange": raw_item.get("exchange"),
                "board": raw_item.get("board"),
                "status": raw_item["status"],
                "status_cn": raw_item.get("status_cn"),
                "meeting_date": (
                    raw_item["meeting_date"].date().isoformat()
                    if raw_item.get("meeting_date")
                    else None
                ),
                "announce_date": (
                    raw_item["announce_date"].date().isoformat()
                    if raw_item.get("announce_date")
                    else None
                ),
                "listing_date": (
                    raw_item["listing_date"].date().isoformat()
                    if raw_item.get("listing_date")
                    else None
                ),
                "underwriter": raw_item.get("underwriter"),
                "share_count": raw_item.get("share_count"),
                "deal_size_cny": raw_item.get("deal_size_cny"),
            },
        }

    def source_event_id(self, raw_item: dict[str, Any]) -> str:
        # Status is part of the key so a status transition (e.g. 未上会
        # → 上会通过) creates a new event row instead of silently mutating
        # the old one — the new alert is what the user wants.
        return make_source_event_id(
            "cn_review", raw_item["code"], raw_item["status"]
        )

    def dedup_key(self, raw_item: dict[str, Any]) -> str:
        return make_dedup_key(self.source_event_id(raw_item))


_STATUS_LABEL = {
    "approved": "approved",
    "rejected": "rejected",
    "scheduled": "scheduled",
    "postponed": "postponed",
    "cancelled": "cancelled",
}
