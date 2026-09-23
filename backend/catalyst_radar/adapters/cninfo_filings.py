"""CNINFO (巨潮资讯网) IPO-filing adapter.

CNINFO is the CSRC-designated official disclosure portal. It carries
prospectuses and IPO-related filings for SSE main + STAR + SZSE main +
ChiNext + BSE in one feed via the public `hisAnnouncement/query` POST
endpoint. We filter to `category=category_sf_szsh` (首发 / first
public offering) and classify each filing by title — the 招股说明书 /
招股意向书 lines are what the prospectus enricher consumes.

The endpoint has no auth and no captcha at <1 req/s. It's served HTTP
(not HTTPS); PDFs come from a separate CDN at static.cninfo.com.cn.
Geo-egress from outside CN is occasionally slow but works; the adapter
treats a fetch failure as best-effort (returns empty items, lets the
sync record a failed source_run).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx

from catalyst_radar.adapters._retry import retry_async
from catalyst_radar.config import settings
from catalyst_radar.dedup import dedup_key as make_dedup_key
from catalyst_radar.dedup import source_event_id as make_source_event_id
from catalyst_radar.logging import get_logger
from catalyst_radar.net_guard import guarded_client

log = get_logger(__name__)

_QUERY_URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
_PDF_BASE = "http://static.cninfo.com.cn/"
_REFERER = "http://www.cninfo.com.cn/new/commonUrl/pageOfSearch?url=disclosure/list/search"
_MAX_PDF_BYTES = 60 * 1024 * 1024

# Title keywords identifying the document types we care about, in order
# of preference for the prospectus enricher. 招股说明书 = formal prospectus;
# 招股意向书 = indicative prospectus (filed earlier in the process). We
# also tag 上市公告书 (listing announcement) and 发行公告 (issue notice)
# for the catalyst signal but only the prospectus-class entries feed the
# LLM extractor.
_DOC_TYPE_RULES = (
    ("招股说明书", "prospectus"),
    ("招股意向书", "prospectus_intent"),
    ("上市公告书", "listing_announcement"),
    ("发行结果公告", "issue_result"),
    ("发行公告", "issue_notice"),
)


def _classify_doc(title: str) -> str | None:
    """Map an announcement title to one of our doc-type tags. Returns None
    for filings unrelated to the IPO event (e.g. management bios, the
    sponsor's tutoring report)."""
    if not title:
        return None
    for keyword, tag in _DOC_TYPE_RULES:
        if keyword in title:
            return tag
    return None


def _exchange_from_seccode(sec_code: str | None) -> str | None:
    """Return SSE / SZSE / BSE for a mainland-CN ticker, None for
    anything malformed. Same prefix logic the akshare adapter uses;
    kept in-module so the two adapters can be reasoned about
    independently."""
    if not sec_code or len(sec_code) != 6 or not sec_code.isdigit():
        return None
    first = sec_code[0]
    if first == "6":
        return "SSE"
    if first in {"0", "3"}:
        return "SZSE"
    if first in {"4", "8", "9"}:
        return "BSE"
    return None


def _parse_time(ms: Any) -> datetime | None:
    """CNINFO returns announcement times as ms-since-epoch ints."""
    try:
        seconds = int(ms) / 1000
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(seconds, tz=UTC)


def normalize_announcements(announcements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pure: raw CNINFO `announcements` array -> filtered + classified
    items for SSE/SZSE only. Each item carries the absolute PDF URL so
    the downloader doesn't have to know CNINFO's CDN layout."""
    out: list[dict[str, Any]] = []
    for a in announcements:
        code = str(a.get("secCode") or "").strip()
        exchange = _exchange_from_seccode(code)
        if exchange is None:
            continue
        title = str(a.get("announcementTitle") or "").strip()
        doc_type = _classify_doc(title)
        if doc_type is None:
            continue
        adjunct = str(a.get("adjunctUrl") or "").strip()
        if not adjunct:
            continue
        out.append(
            {
                "announcement_id": str(a.get("announcementId") or "").strip(),
                "sec_code": code,
                "sec_name": str(a.get("secName") or "").strip(),
                "exchange": exchange,
                "title": title,
                "doc_type": doc_type,
                "announcement_time": _parse_time(a.get("announcementTime")),
                "pdf_url": f"{_PDF_BASE}{adjunct.lstrip('/')}",
                "pdf_size_kb": a.get("adjunctSize"),
            }
        )
    return out


class CninfoFilingsAdapter:
    """Pulls A-share IPO filings from CNINFO. Not a SourceAdapter subclass
    because the upstream interface doesn't fit — CNINFO is paginated and
    we want to expose `fetch_recent` for the sync and `download_pdf` for
    the enricher as two different surface methods."""

    source_name = "cninfo"
    schema_name = "cninfo.hisAnnouncement.v1"

    def __init__(
        self,
        timeout: int | None = None,
        page_size: int = 30,
        min_request_interval: float = 1.0,
    ) -> None:
        self.timeout = timeout or settings.cninfo_request_timeout_seconds
        self.page_size = page_size
        self.min_request_interval = min_request_interval
        self._last_request_at: float = 0.0

    async def _throttle(self) -> None:
        """Cap upstream QPS — CNINFO is sensitive to bursts."""
        now = asyncio.get_event_loop().time()
        wait = self.min_request_interval - (now - self._last_request_at)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_request_at = asyncio.get_event_loop().time()

    async def fetch_page(self, page_num: int = 1) -> dict[str, Any] | None:
        """Single hisAnnouncement/query POST. Returns the parsed JSON or
        None on transport/HTTP failure."""
        await self._throttle()
        data = {
            "pageNum": str(page_num),
            "pageSize": str(self.page_size),
            "column": "szse",  # required but server returns cross-exchange anyway
            "tabName": "fulltext",
            "category": "category_sf_szsh",
        }

        async def _post() -> httpx.Response:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                return await client.post(
                    _QUERY_URL,
                    data=data,
                    headers={
                        "User-Agent": settings.scraper_user_agent,
                        "Referer": _REFERER,
                        "X-Requested-With": "XMLHttpRequest",
                        "Accept": "application/json, text/plain, */*",
                    },
                )

        try:
            # Geo-egress from outside CN is occasionally slow / drops mid-read;
            # a couple of retries with a short backoff recovers transient
            # timeouts and resets without wasting the whole beat tick. The
            # outer fail-soft (return None) still catches a genuine outage.
            resp = await retry_async(
                _post, attempts=3, base_delay=1.0, max_delay=4.0, label="cninfo"
            )
        except httpx.HTTPError as exc:
            log.warning("cninfo_fetch_failed", page=page_num, error=repr(exc))
            return None
        if resp.status_code != 200:
            log.warning("cninfo_fetch_http", page=page_num, status=resp.status_code)
            return None
        try:
            return resp.json()
        except ValueError:
            log.warning("cninfo_fetch_non_json", page=page_num)
            return None

    async def fetch_recent(self, max_pages: int = 2) -> list[dict[str, Any]]:
        """Pull the first `max_pages` of 首发 announcements (most-recent
        first) and return normalized items for SSE/SZSE only. Default 2
        pages × 30 rows = 60 most recent A-share IPO filings, well above
        the 30-min sync cadence's headroom."""
        items: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            data = await self.fetch_page(page)
            if data is None:
                break
            announcements = data.get("announcements") or []
            if not announcements:
                break
            items.extend(normalize_announcements(announcements))
        return items

    async def download_pdf(self, url: str) -> bytes | None:
        """Stream-download a prospectus PDF with a hard byte ceiling so a
        runaway file can't exhaust memory. CNINFO PDFs in our sample run
        2-10 MB; the 60 MB cap is generous."""
        await self._throttle()
        try:
            async with guarded_client(
                timeout=httpx.Timeout(self.timeout, read=settings.cninfo_pdf_download_timeout),
                follow_redirects=True,
            ) as client:
                ua = {"User-Agent": settings.scraper_user_agent}
                async with client.stream("GET", url, headers=ua) as r:
                    if r.status_code != 200:
                        log.warning("cninfo_pdf_http", url=url, status=r.status_code)
                        return None
                    buf = bytearray()
                    async for chunk in r.aiter_bytes():
                        buf.extend(chunk)
                        if len(buf) > _MAX_PDF_BYTES:
                            log.warning("cninfo_pdf_too_large", url=url)
                            return None
                    return bytes(buf)
        except httpx.HTTPError as exc:
            log.warning("cninfo_pdf_download_failed", url=url, error=repr(exc))
            return None

    @staticmethod
    def source_event_id(item: dict[str, Any]) -> str:
        # Stable across pages and reruns — the announcementId is CNINFO's
        # primary key for the filing.
        return make_source_event_id("cninfo", "ipo", item["announcement_id"])

    @staticmethod
    def dedup_key(item: dict[str, Any]) -> str:
        return make_dedup_key(CninfoFilingsAdapter.source_event_id(item))
