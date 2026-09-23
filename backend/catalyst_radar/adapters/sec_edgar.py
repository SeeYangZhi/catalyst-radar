"""SEC EDGAR adapter — resolves a US IPO's registration filing and
extracts the prospectus-summary text for LLM one-liner + market cap.

EDGAR is a free public API but requires a descriptive User-Agent and
fair-access rate limits. The full-text search (efts) is Elasticsearch-
backed and intermittently 500s, so search is retried. Parsing of the
filing HTML is a pure function (fixture-tested, no network).
"""

import asyncio
import html
import re
from typing import Any

import httpx

from catalyst_radar.config import settings
from catalyst_radar.logging import get_logger

log = get_logger(__name__)

_FTS = "https://efts.sec.gov/LATEST/search-index"
_SUB = "https://data.sec.gov/submissions/CIK{cik10}.json"
_ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"
_FORMS = ("424B4", "424B1", "424B3", "S-1/A", "S-1", "F-1/A", "F-1", "497", "485BPOS", "485APOS")

# Where the business description tends to start in a prospectus.
_ANCHORS = (
    "prospectus summary",
    "our company",
    "company overview",
    "business overview",
    "our business",
    "overview of our business",
)

# ETF/trust 497-filing patterns for extracting fund name + objective.
_497_PATTERNS = (
    # "Polen 5Perspectives Small-Mid Growth ETF (the \"Fund\") seeks to..."
    r"([A-Z][A-Za-z0-9\s\-]+ETF)\s*\(\s*the\s*['\"]?Fund['\"]?\s*\)\s*seeks?\s+to\s+(.+?)(?:\s{2,}|\Z)",
    # "Investment Objective ... Fund seeks to..."
    r"Investment Objective\s*.*?(\w[\w\s\-]+ETF).*?seeks?\s+to\s+(.+?)(?:\s{2,}|\Z)",
    # "The Fund seeks to achieve..."
    r"(?:The\s+)?Fund\s+seeks?\s+to\s+(.+?)(?:\s{2,}|\Z)",
)


class SecEdgarConfigError(RuntimeError):
    pass


def _headers() -> dict[str, str]:
    ua = settings.sec_edgar_user_agent.strip()
    if not ua:
        raise SecEdgarConfigError("SEC_EDGAR_USER_AGENT is not configured")
    return {"User-Agent": ua, "Accept-Encoding": "gzip, deflate"}


def _visible_text(raw_html: str) -> str:
    """Strip script/style/tags -> collapsed visible text."""
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw_html)
    # Tolerate partial tags at slice boundaries (leading/trailing).
    s = re.sub(r"^[^<]*?>", " ", s)
    s = re.sub(r"<[^>]*$", " ", s)
    no_tags = re.sub(r"(?s)<[^>]+>", " ", s)
    return html.unescape(re.sub(r"\s+", " ", no_tags)).strip()


def extract_summary(raw_html: str, max_chars: int = 18000) -> str:
    """Pure: prospectus HTML -> bounded summary slice for the LLM.

    Anchors on the first business-summary heading; falls back to the
    head of the document body when no anchor is found.
    """
    text = _visible_text(raw_html)
    low = text.lower()
    idx = -1
    for kw in _ANCHORS:
        i = low.find(kw)
        if i != -1 and (idx == -1 or i < idx):
            idx = i
    start = max(0, idx) if idx != -1 else 0
    return text[start : start + max_chars].strip()


def extract_497_summary(raw_html: str) -> str:
    """Pure: ETF/trust 497 filing HTML -> 'Fund Name seeks to...' sentence.

    497 filings are plain HTML (not XBRL) and contain a concise
    investment-objective statement near the top.  Returns empty string
    when no pattern matches.
    """
    text = _visible_text(raw_html)
    for pat in _497_PATTERNS:
        m = re.search(pat, text, re.I | re.S)
        if m:
            # Pattern may capture 1 or 2 groups depending on regex
            groups = m.groups()
            if len(groups) == 2:
                fund, objective = groups
                return f"{fund.strip()} seeks to {objective.strip()}."
            if len(groups) == 1:
                return f"The Fund seeks to {groups[0].strip()}."
    return ""


class SecEdgarAdapter:
    source_name = "sec_edgar"

    def __init__(self, timeout: int | None = None) -> None:
        self.timeout = timeout or settings.sec_edgar_request_timeout_seconds

    async def _get(self, url: str, *, params: dict | None = None) -> httpx.Response | None:
        # SEC fair-access: small volume, retry transient 5xx/429 with backoff.
        for attempt in range(1, 4):
            try:
                async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                    resp = await client.get(url, params=params, headers=_headers())
                if resp.status_code == 200:
                    return resp
                if resp.status_code not in (429, 500, 502, 503):
                    log.warning("sec_edgar_http", url=url, status=resp.status_code)
                    return None
            except httpx.HTTPError as exc:
                log.warning("sec_edgar_error", url=url, error=repr(exc))
            await asyncio.sleep(min(2**attempt, 6))
        return None

    async def search_cik(self, company_name: str) -> str | None:
        """First registration-filing CIK whose display name matches.

        The EFTS full-text search indexes individual exhibit documents
        (EX-99.1, EX-23.1, etc.), not root filing forms.  Filtering by
        ``forms`` often returns 0 hits for companies whose exhibits are
        indexed under different file_type values.  Searching without the
        ``forms`` filter reliably resolves the CIK; ``latest_filing()``
        then picks the best registration form via the submissions endpoint.

        We search without quotes first (broader match) and fall back to
        quoted exact phrase if needed.  This handles cases like
        "Aperture AC Unit" where the SEC display name is "Aperture AC".
        """
        # Try unquoted search first (broader match)
        for q in (company_name, f'"{company_name}"'):
            resp = await self._get(_FTS, params={"q": q})
            if resp is None:
                continue
            try:
                hits = resp.json().get("hits", {}).get("hits", [])
            except ValueError:
                continue
            norm = re.sub(r"[^a-z0-9]", "", company_name.lower())
            for h in hits:
                src = h.get("_source", {})
                names = " ".join(src.get("display_names", [])).lower()
                if norm[:12] and norm[:12] in re.sub(r"[^a-z0-9]", "", names):
                    ciks = src.get("ciks") or []
                    if ciks:
                        return str(int(ciks[0]))  # canonical, unpadded
        return None

    async def latest_filing(self, cik: str) -> dict[str, Any] | None:
        cik_int = str(int(cik))
        resp = await self._get(_SUB.format(cik10=cik_int.zfill(10)))
        if resp is None:
            return None
        try:
            data = resp.json()
        except ValueError:
            return None
        r = data.get("filings", {}).get("recent", {})
        rows = list(
            zip(
                r.get("form", []),
                r.get("filingDate", []),
                r.get("accessionNumber", []),
                r.get("primaryDocument", []),
                strict=False,
            )
        )
        rank = {f: i for i, f in enumerate(_FORMS)}
        best: tuple[int, str, dict] | None = None
        for form, date, acc, doc in rows:
            if form not in rank or not doc:
                continue
            key = (-rank[form], date)
            cand = {
                "form": form,
                "filing_date": date,
                "doc_url": _ARCHIVE.format(cik=cik_int, acc=acc.replace("-", ""), doc=doc),
            }
            if best is None or key > best[:2]:
                best = (key[0], key[1], cand)  # type: ignore[assignment]
        return best[2] if best else None

    async def fetch_summary(self, company_name: str) -> dict[str, Any] | None:
        """Resolve filing and return {summary, filing_url, form, filing_date}."""
        cik = await self.search_cik(company_name)
        if not cik:
            return None
        filing = await self.latest_filing(cik)
        if not filing:
            return None
        resp = await self._get(filing["doc_url"])
        if resp is None:
            return None
        # Use 497-specific extractor for ETF/trust filings
        if filing["form"] == "497":
            summary = extract_497_summary(resp.text)
        else:
            summary = extract_summary(resp.text)
        if not summary:
            return None
        return {
            "summary": summary,
            "filing_url": filing["doc_url"],
            "form": filing["form"],
            "filing_date": filing["filing_date"],
            "cik": cik,
        }
