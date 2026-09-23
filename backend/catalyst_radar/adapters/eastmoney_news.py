"""Per-ticker A-share / BSE news via akshare's Eastmoney wrapper.

``akshare.stock_news_em(symbol)`` is a server-side keyword search by 6-digit
code against Eastmoney's news index — the same upstream we already use for the
IPO calendar (``akshare_ipo.py``) and the CSRC review feed
(``akshare_ipo_review.py``), just a different endpoint. It slots into the
per-company catalyst loop exactly where EODHD news sits, but for CN names EODHD
doesn't meaningfully cover.

The reason this source exists: every item carries ``发布时间`` (publish
datetime, second precision, Asia/Shanghai). That gives the catalyst pipeline a
real ``date`` for CN news, so items flow through the freshness gate instead of
tripping ``drop_undated_news`` the way ``web_search``'s static-product-page
rediscoveries did (Unitree H2/R1/DigitalServo). Columns returned:
``关键词, 新闻标题, 新闻内容, 发布时间, 文章来源, 新闻链接``. ``新闻内容`` is a
snippet (typically <200 chars) — the classifier handles short text fine
(EODHD content is similarly short).

Free, public, no auth. akshare runs blocking I/O against Eastmoney through
pandas; we offload it to a thread so the asyncio loop is never blocked.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pandas as pd

from catalyst_radar.adapters._retry import retry_sync
from catalyst_radar.adapters.base import FetchResult, SourceAdapter
from catalyst_radar.dedup import dedup_key as make_dedup_key
from catalyst_radar.dedup import normalize_url, stable_hash
from catalyst_radar.dedup import source_event_id as make_source_event_id
from catalyst_radar.logging import get_logger

log = get_logger(__name__)

# stock_news_em keys off the bare 6-digit code; tc.symbol already is that for
# CN listings. These are the exchanges Eastmoney's news index covers — also
# the set the web_search sweep uses to decide a listed CN name is already
# owned here (see catalyst_sources._sync_websearch_catalysts).
_CN_EXCHANGES = frozenset({"SSE", "SZSE", "BSE"})


def supports(exchange: str | None) -> bool:
    """True when this exchange's news is served by Eastmoney's per-ticker
    endpoint (i.e. a mainland-China listing)."""
    return (exchange or "").strip().upper() in _CN_EXCHANGES


def _to_iso_shanghai(raw: Any) -> str | None:
    """``'2026-06-03 21:31:00'`` (Asia/Shanghai, naive) -> ISO string with a
    ``+08:00`` offset. The downstream parser (``catalyst_classify._news_dt``)
    assumes UTC for naive timestamps, so attaching the real offset keeps the
    freshness-gate age from skewing by 8 hours."""
    s = str(raw or "").strip()
    if not s:
        return None
    try:
        dt = pd.to_datetime(s)
    except (ValueError, TypeError):
        return None
    if pd.isna(dt):
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + "+08:00"


def normalize_rows(df: pd.DataFrame) -> list[dict[str, Any]]:
    """Pure: Eastmoney news DataFrame -> list of normalized
    ``{title, content, link, date, source_label}`` items. Drops rows with no
    title (the only field the downstream pipeline cannot do without)."""
    items: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        title = str(row.get("新闻标题", "")).strip()
        if not title:
            continue
        link = str(row.get("新闻链接", "")).strip() or None
        items.append(
            {
                "title": title,
                "content": str(row.get("新闻内容", "")).strip(),
                "link": link,
                "date": _to_iso_shanghai(row.get("发布时间")),
                "source_label": str(row.get("文章来源", "")).strip() or None,
            }
        )
    return items


class EastmoneyNewsAdapter(SourceAdapter):
    """Candidate catalyst source. Like every news adapter, never notify
    directly — deterministic prefilters + LLM classification gate alerts."""

    source_name = "eastmoney_news"
    schema_name = "akshare.stock_news_em.v1"
    source_url = "https://so.eastmoney.com/news/s"

    async def fetch(self, target: str) -> FetchResult:
        """``target`` is the 6-digit A-share / BSE code (e.g. ``688797``)."""
        try:
            df = await asyncio.to_thread(self._fetch_blocking, target)
        except Exception as exc:  # noqa: BLE001 - one bad sync must not abort the run
            log.warning("eastmoney_news_fetch_failed", code=target, error=repr(exc))
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
            payload={"code": target, "row_count": int(len(df))},
            items=items,
        )

    # Eastmoney rate-limits aggressive callers and occasionally truncates a
    # response mid-stream; a fresh retry after a generous backoff almost
    # always recovers. 3 attempts, 5s/20s backoff (shared retry default) —
    # conservative on purpose, short retries make rate-limiting worse.
    @classmethod
    def _fetch_blocking(cls, code: str) -> pd.DataFrame:
        import akshare as ak

        return retry_sync(
            lambda: ak.stock_news_em(symbol=code),
            attempts=3,
            base_delay=5.0,
            max_delay=20.0,
            # akshare wraps `requests` — retry whatever Eastmoney throws (the
            # prior hand-rolled loop's behaviour), not just the httpx-oriented
            # transient set. Outer fail-soft still catches a real outage.
            retry_on=(Exception,),
            label="eastmoney_news",
        )

    def normalize(self, raw_item: dict[str, Any]) -> dict[str, Any]:
        # fetch already emits the {title, content, link, date} shape the
        # pipeline consumes; nothing left to reshape.
        return raw_item

    def source_event_id(self, raw_item: dict[str, Any]) -> str:
        # Canonical URL so any mobile/desktop variants of the same article
        # collapse to one sid; fall back to title+date (not title alone) so
        # distinct link-less items with the same headline don't collide.
        link = raw_item.get("link")
        basis = (
            normalize_url(link)
            if link
            else f"{raw_item.get('title', '')}|{raw_item.get('date', '')}"
        )
        return make_source_event_id("eastmoney", "news", stable_hash(basis))

    def dedup_key(self, raw_item: dict[str, Any]) -> str:
        link = raw_item.get("link")
        return make_dedup_key(normalize_url(link) if link else raw_item.get("title"))
