"""HKEXnews prospectus adapter (Phase 9.6b).

The HKEX "New Listing Information (Main Board)" page is server-rendered
as a clean table: Stock Code | Stock Name | New Listing Announcements |
Prospectuses | Allotment Results, each linking a PDF on
www1.hkexnews.hk. We parse that (pure, fixture-tested), then download
the prospectus PDF and extract a bounded SUMMARY-chapter slice for the
LLM. HK prospectuses are large (10-40MB) and HKEXnews is slow, so the
download is generously timed + size-capped, and only the first pages
(front matter, where SUMMARY lives) are text-extracted.
"""

import io
import json
import re
from typing import Any

import httpx

from catalyst_radar.adapters.hk_codes import pad_code
from catalyst_radar.config import settings
from catalyst_radar.logging import get_logger
from catalyst_radar.net_guard import guarded_client

log = get_logger(__name__)

_MAX_PDF_BYTES = 60 * 1024 * 1024
_ANCHORS = (
    "overview of our",
    "our business",
    "business overview",
    "our company",
    "we are a",
    "summary",
)


def _norm_code(raw: str | None) -> str | None:
    """Leading-zero-stripped variant of the shared canonical code, used
    as the key when matching the New Listing Information table."""
    s = pad_code(raw)
    return str(int(s)) if s else None


def parse_new_listings(html: str) -> dict[str, dict[str, str]]:
    """Pure: New-Listing-Information HTML -> {code: {name, prospectus_url,
    announcement_url, allotment_url}}. Code normalised (no leading zeros)."""
    out: dict[str, dict[str, str]] = {}
    for tr in re.findall(r"(?is)<tr>(.*?)</tr>", html):
        tds = re.findall(r"(?is)<td[^>]*>(.*?)</td>", tr)
        if len(tds) < 5:
            continue
        code = _norm_code(re.sub(r"(?s)<[^>]+>", "", tds[0]))
        name = re.sub(r"(?s)<[^>]+>", " ", tds[1])
        name = re.sub(r"\s+", " ", name).strip()
        if not code or not name:
            continue

        def _href(cell: str) -> str:
            m = re.search(r'href="([^"]+\.pdf)"', cell, re.I)
            return m.group(1) if m else ""

        out[code] = {
            "name": name,
            "announcement_url": _href(tds[2]),
            "prospectus_url": _href(tds[3]),
            "allotment_url": _href(tds[4]),
        }
    return out


def _slice_summary(text: str, max_chars: int = 18000) -> str:
    """Pure: collapse + anchor on the first business-summary phrase."""
    text = re.sub(r"\s+", " ", text or "").strip()
    low = text.lower()
    idx = -1
    for kw in _ANCHORS:
        i = low.find(kw)
        if i != -1 and (idx == -1 or i < idx):
            idx = i
    start = max(0, idx) if idx != -1 else 0
    return text[start : start + max_chars].strip()


def extract_summary_from_pdf(pdf_bytes: bytes, max_pages: int = 45, max_chars: int = 18000) -> str:
    """Pure: prospectus PDF bytes -> bounded SUMMARY-chapter text slice.

    Only the front matter is read (SUMMARY is early); anchors on the
    first business-summary phrase, else falls back to the head text.
    """
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception as exc:  # noqa: BLE001 - corrupt/partial PDF
        log.warning("hkex_pdf_unreadable", error=repr(exc))
        return ""
    parts: list[str] = []
    for page in reader.pages[:max_pages]:
        try:
            parts.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001 - one bad page must not abort
            parts.append("")
    return _slice_summary(" ".join(parts), max_chars)


class HkexProspectusAdapter:
    source_name = "hkexnews"

    def __init__(self, timeout: int | None = None) -> None:
        self.timeout = timeout or settings.hkex_request_timeout_seconds
        self.listings_url = settings.hkex_new_listings_url

    async def _fetch_new_listings_html(self) -> str | None:
        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                resp = await client.get(
                    self.listings_url, headers={"User-Agent": settings.scraper_user_agent}
                )
            return resp.text if resp.status_code == 200 else None
        except httpx.HTTPError as exc:
            log.warning("hkex_nli_failed", error=repr(exc))
            return None

    async def _download_pdf(self, url: str) -> bytes | None:
        # HKEXnews is slow and prospectuses are large; allow a long read
        # but cap total bytes so a runaway file can't exhaust memory.
        try:
            async with guarded_client(
                timeout=httpx.Timeout(self.timeout, read=settings.hkex_prospectus_download_timeout),
                follow_redirects=True,
            ) as client:
                ua = {"User-Agent": settings.scraper_user_agent}
                async with client.stream("GET", url, headers=ua) as r:
                    if r.status_code != 200:
                        return None
                    buf = bytearray()
                    async for chunk in r.aiter_bytes():
                        buf.extend(chunk)
                        if len(buf) > _MAX_PDF_BYTES:
                            log.warning("hkex_pdf_too_large", url=url)
                            return None
                    return bytes(buf)
        except httpx.HTTPError as exc:
            log.warning("hkex_pdf_download_failed", url=url, error=repr(exc))
            return None

    async def fetch_prospectus(self, stock_code: str) -> dict[str, Any] | None:
        """Resolve the prospectus for a HK stock code and return
        {summary, filing_url, doc_type}.

        Tries the New Listing Information page first (upcoming/recent IPOs),
        then falls back to the archive search (already-listed companies)."""
        code = _norm_code(stock_code)
        if not code:
            return None

        # 1. Try New Listing Information page (fast path for recent IPOs)
        html = await self._fetch_new_listings_html()
        if html:
            listings = parse_new_listings(html)
            if not listings:
                # Either the page format changed (regex no longer matches
                # the table) or HKEX served an empty NLI. Either way the
                # silent-zero is hard to diagnose without a signal.
                log.warning("hkex_nli_zero_rows", html_bytes=len(html))
            listing = listings.get(code)
            if listing and listing.get("prospectus_url"):
                pdf = await self._download_pdf(listing["prospectus_url"])
                if pdf:
                    summary = extract_summary_from_pdf(pdf)
                    if summary:
                        return {
                            "summary": summary,
                            "filing_url": listing["prospectus_url"],
                            "doc_type": "prospectus",
                        }

        # 2. Fallback: search HKEX archive for already-listed companies
        return await self._fetch_from_archive(code)

    async def _fetch_from_archive(self, code: str) -> dict[str, Any] | None:
        """Search HKEX titlesearch archive for a listed company's prospectus."""
        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                stock_id = await self._resolve_stock_id(code, client)
                if not stock_id:
                    return None
                pdf_url = await self._search_archive(stock_id, client)
                if not pdf_url:
                    return None
                pdf = await self._download_pdf(pdf_url)
                if not pdf:
                    return None
                summary = extract_summary_from_pdf(pdf)
                if not summary:
                    return None
                return {
                    "summary": summary,
                    "filing_url": pdf_url,
                    "doc_type": "prospectus",
                }
        except httpx.HTTPError as exc:
            log.warning("hkex_archive_search_failed", code=code, error=repr(exc))
            return None

    async def _resolve_stock_id(self, code: str, client: httpx.AsyncClient) -> int | None:
        """Query HKEX prefix.do autocomplete to get the internal stockId."""
        try:
            resp = await client.get(
                "https://www1.hkexnews.hk/search/prefix.do",
                params={
                    "callback": "callback",
                    "lang": "EN",
                    "type": "A",
                    "name": pad_code(code) or code,
                    "market": "SEHK",
                },
                timeout=15,
            )
            json_str = resp.text.replace("callback(", "").rstrip(");\r\n")
            data = json.loads(json_str)
            if data.get("stockInfo"):
                return data["stockInfo"][0]["stockId"]
        except Exception:  # noqa: BLE001
            pass
        return None

    async def _search_archive(self, stock_id: int, client: httpx.AsyncClient) -> str | None:
        """Search HKEX titlesearch for all filings of a stock, return the
        GLOBAL OFFERING / prospectus PDF URL if found."""
        try:
            resp = await client.get(
                "https://www1.hkexnews.hk/search/titlesearch.xhtml",
                params={
                    "lang": "EN",
                    "category": "0",
                    "market": "SEHK",
                    "searchType": "1",
                    "t1code": "-2",
                    "t2Gcode": "-2",
                    "t2code": "-2",
                    "stockId": str(stock_id),
                    "from": "20250101",
                    "to": "20261231",
                },
                timeout=30,
            )
            if resp.status_code != 200 or len(resp.text) < 5000:
                return None

            # Extract rows and look for GLOBAL OFFERING or Prospectus
            rows = re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", resp.text)
            for row in rows:
                cells = re.findall(r"(?is)<td[^>]*>(.*?)</td>", row)
                if len(cells) < 4:
                    continue
                doc_cell = cells[3]
                pdf_match = re.search(r'href="([^"]*\.pdf)"', doc_cell, re.I)
                if not pdf_match:
                    continue
                # Look for prospectus-related titles
                title_match = re.search(r">([^<]+)</a>", doc_cell)
                title = title_match.group(1).strip() if title_match else ""
                low = title.lower()
                if "global offering" in low or "prospectus" in low:
                    url = pdf_match.group(1)
                    # Ensure absolute URL
                    if url.startswith("/"):
                        url = f"https://www1.hkexnews.hk{url}"
                    return url
            return None
        except Exception:  # noqa: BLE001
            return None
