"""Structured financials extraction from an earnings-report URL.

When the catalyst classifier tags an item as ``earnings_report`` (or
``guidance``), the raw notification is useless on its own — it only
references the document, not its contents (see Shunsin Q2 2025 incident:
the alert said "report posted" but never extracted revenue, EPS, YoY,
etc.). This module closes that gap.

Flow per call:
1. Fetch the URL (PDF → ``pypdf`` text; HTML → naive tag strip).
2. Truncate to ``catalyst_enrich_max_chars`` to bound the LLM input.
3. POST to OpenAI Responses with a strict JSON schema asking for
   ``revenue``, ``revenue_yoy_pct``, ``net_profit``, ``eps``,
   ``key_drivers``, etc.

Failure is *never* fatal to the catalyst pipeline — extraction is
best-effort enrichment, not a gate on the notification.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import Any

import httpx
import pypdf
from openai import APIError, AsyncOpenAI

from catalyst_radar.config import settings
from catalyst_radar.logging import get_logger
from catalyst_radar.net_guard import guarded_client
from catalyst_radar.prompt_context import current_date_line
from catalyst_radar.schemas.llm import ConsensusSearch, EarningsExtraction
from catalyst_radar.services.openai_classifier import reasoning_off_for
from catalyst_radar.services.url_liveness import filter_live_sources

log = get_logger(__name__)

_SYSTEM = (
    "You extract structured quarterly earnings financials from a single "
    "company report (PDF or HTML text) so a trader gets an actionable "
    "summary. Use null for any numeric field not stated in the document. "
    "Numeric values in their native units (e.g. 'thousands of NT$') — state "
    "the unit in `currency`. `headline` is one sentence including direction "
    "and YoY % for revenue and net profit when both are known. `key_drivers` "
    "should be 3-5 short bullets the trader would care about (margin moves, "
    "non-operating swings, FX impact, guidance changes). When guidance is "
    "unchanged, set guidance_change to an empty string."
)



_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t]+")
_NL = re.compile(r"\n{3,}")

_CONSENSUS_SYSTEM = (
    "You find analyst consensus forecasts (revenue, EPS, net profit) for a "
    "single public company for a specific reporting period, so a trader can "
    "compare them to the company's reported actuals. Use reputable analyst "
    "aggregators (Yahoo Finance, Zacks, StockAnalysis, Investing.com, "
    "Refinitiv, FactSet, broker research notes, the company's own pre-results "
    "commentary). Do NOT use social media, message boards, or unverified blogs. "
    "If you cannot find a reliable consensus from reputable sources, return "
    "all numeric fields as null and an empty sources list — do NOT invent or "
    "guess. Numeric values in their native units (state the unit in "
    "`currency`, which may be an empty string when no numeric is reported). "
    "Up to 3 sources, ordered most-authoritative first. Use an empty string "
    "for `notes` when there is nothing to add."
)



@dataclass(slots=True)
class EnrichmentResult:
    status: str  # completed | failed | skipped
    financials: dict[str, Any] | None = None
    error: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    fetched_bytes: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ConsensusResult:
    """Analyst-consensus search result from OpenAI web_search. `status` is
    'completed' when the call succeeded (even if every numeric field is
    null — that means "we looked, found nothing reputable"). Sources are
    captured so the alert can cite where the numbers came from instead of
    making the trader trust a black-box number."""

    status: str  # completed | failed | skipped
    revenue: float | None = None
    eps: float | None = None
    net_profit: float | None = None
    currency: str | None = None
    period_label: str | None = None
    sources: list[dict[str, str]] = field(default_factory=list)
    notes: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    error: str | None = None


def _strip_html(html: str) -> str:
    """Best-effort tag strip. We are not parsing structure, just feeding the
    LLM readable text — a regex stripper handles the common cases and never
    raises (BeautifulSoup is not a backend dep)."""
    no_script = re.sub(
        r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.IGNORECASE | re.DOTALL
    )
    text = _TAG.sub(" ", no_script)
    text = _WS.sub(" ", text)
    return _NL.sub("\n\n", text).strip()


def _pdf_to_text(blob: bytes) -> str:
    """Extract text from every page; concatenate with page markers so the LLM
    can locate the income statement in context."""
    reader = pypdf.PdfReader(io.BytesIO(blob))
    chunks: list[str] = []
    for i, page in enumerate(reader.pages):
        try:
            text = page.extract_text() or ""
        except Exception:  # noqa: BLE001 - one bad page must not abort
            text = ""
        if text.strip():
            chunks.append(f"=== PAGE {i + 1} ===\n{text}")
    return "\n\n".join(chunks)


async def _fetch(url: str, timeout_seconds: int) -> tuple[bytes, str]:
    """Return (body, content_type). Raises on HTTP error."""
    async with guarded_client(timeout=timeout_seconds, follow_redirects=True) as client:
        resp = await client.get(url)
    resp.raise_for_status()
    ctype = (resp.headers.get("content-type") or "").lower()
    return resp.content, ctype


def _is_pdf(url: str, content_type: str, body: bytes) -> bool:
    """Detect PDF by content-type, URL suffix, or magic bytes — IIS servers
    sometimes return generic content-types for PDF assets."""
    if "pdf" in content_type:
        return True
    if url.lower().split("?", 1)[0].endswith(".pdf"):
        return True
    return body[:5] == b"%PDF-"


async def extract_from_url(
    url: str,
    *,
    company_name: str,
    symbol: str,
    api_key: str,
    model: str | None = None,
    max_chars: int | None = None,
    fetch_timeout: int | None = None,
    max_output_tokens: int | None = None,
) -> EnrichmentResult:
    """Fetch + extract + LLM-summarize one earnings report URL.

    Returns an EnrichmentResult with status='completed' and populated
    `financials` on success, otherwise status='failed' with an `error`.
    Caller is responsible for budget accounting and for never letting a
    failed enrichment block the underlying notification."""
    if not api_key:
        return EnrichmentResult("failed", error="OPENAI_API_KEY not configured")
    if not url:
        return EnrichmentResult("skipped", error="no url")

    model = model or settings.catalyst_enrich_model
    max_chars = max_chars or settings.catalyst_enrich_max_chars
    fetch_timeout = fetch_timeout or settings.catalyst_enrich_fetch_timeout_seconds
    max_output_tokens = max_output_tokens or settings.catalyst_enrich_max_output_tokens

    try:
        body, ctype = await _fetch(url, timeout_seconds=fetch_timeout)
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        return EnrichmentResult("failed", error=f"fetch_failed: {exc!r}")

    fetched_bytes = len(body)
    try:
        if _is_pdf(url, ctype, body):
            text = _pdf_to_text(body)
        else:
            text = _strip_html(body.decode("utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001 - parse failure must not raise
        return EnrichmentResult(
            "failed", error=f"parse_failed: {exc!r}", fetched_bytes=fetched_bytes
        )

    if not text or len(text) < 200:
        return EnrichmentResult(
            "failed",
            error=f"document_text_too_short ({len(text)} chars)",
            fetched_bytes=fetched_bytes,
        )

    text = text[:max_chars]
    user = (
        f"Company: {company_name} ({symbol})\n"
        f"Source URL: {url}\n\n"
        f"DOCUMENT TEXT:\n{text}"
    )
    client = AsyncOpenAI(api_key=api_key, timeout=settings.openai_timeout_seconds)
    try:
        response = await client.responses.parse(
            model=model,
            input=[
                {"role": "system", "content": _SYSTEM + current_date_line()},
                {"role": "user", "content": user},
            ],
            text_format=EarningsExtraction,
            max_output_tokens=max_output_tokens,
            reasoning=reasoning_off_for(model),
        )
    except APIError as exc:
        return EnrichmentResult(
            "failed", error=f"llm_http_error: {exc!r}", fetched_bytes=fetched_bytes
        )
    finally:
        # Deterministic close — the SDK's __del__ otherwise schedules
        # aclose() on a later Celery task's loop (log noise on teardown).
        await client.close()

    usage = response.usage
    meta = {
        "fetched_bytes": fetched_bytes,
        "input_tokens": getattr(usage, "input_tokens", None),
        "output_tokens": getattr(usage, "output_tokens", None),
    }
    if response.status != "completed" or response.output_parsed is None:
        return EnrichmentResult(
            "failed",
            error=str(response.incomplete_details or response.status),
            **meta,
        )
    return EnrichmentResult(
        "completed",
        financials=response.output_parsed.model_dump(),
        **meta,
    )


async def find_consensus(
    *,
    company_name: str,
    symbol: str,
    period_label: str,
    api_key: str,
    model: str | None = None,
    timeout_seconds: int | None = None,
    max_output_tokens: int | None = None,
) -> ConsensusResult:
    """Look up analyst-consensus forecasts (revenue / EPS / net profit) for
    the cited reporting period via OpenAI Responses + web_search.

    Always returns a result — never raises. A `status='completed'` result
    with all-null numerics means "we searched reputable sources and found
    nothing reliable", which is the correct outcome for thinly-covered
    names (e.g. Shunsin 6451.TW). The caller renders consensus only when
    at least one numeric field is populated."""
    if not api_key:
        return ConsensusResult("failed", error="OPENAI_API_KEY not configured")
    if not period_label:
        return ConsensusResult("skipped", error="no period_label")

    model = model or settings.catalyst_enrich_model
    timeout_seconds = (
        timeout_seconds or settings.catalyst_enrich_consensus_timeout_seconds
    )
    max_output_tokens = (
        max_output_tokens or settings.catalyst_enrich_consensus_max_output_tokens
    )

    user = (
        f"Company: {company_name} ({symbol})\n"
        f"Reporting period: {period_label}\n\n"
        "Find the analyst consensus forecast for this quarter — the numbers "
        "analysts were expecting BEFORE the results were released."
    )
    client = AsyncOpenAI(api_key=api_key, timeout=timeout_seconds)
    try:
        response = await client.responses.parse(
            model=model,
            input=[
                {"role": "system", "content": _CONSENSUS_SYSTEM + current_date_line()},
                {"role": "user", "content": user},
            ],
            text_format=ConsensusSearch,
            tools=[{"type": "web_search"}],
            max_output_tokens=max_output_tokens,
            reasoning=reasoning_off_for(model),
        )
    except APIError as exc:
        return ConsensusResult("failed", error=f"consensus_http_error: {exc!r}")
    finally:
        await client.close()  # see maybe_extract: avoid GC-time aclose noise

    usage = response.usage
    meta = {
        "input_tokens": getattr(usage, "input_tokens", None),
        "output_tokens": getattr(usage, "output_tokens", None),
    }
    if response.status != "completed" or response.output_parsed is None:
        return ConsensusResult(
            "failed",
            error=str(response.incomplete_details or response.status),
            **meta,
        )

    parsed = response.output_parsed
    sources = [
        {"name": s.name, "url": s.url}
        for s in parsed.sources[:3]
        if s.name.strip() and s.url.strip()
    ]
    # Strip out hallucinated/dead citation URLs before they appear in the
    # alert. The numerics + notes still render — only the dead link is dropped.
    sources = await filter_live_sources(sources)
    return ConsensusResult(
        "completed",
        revenue=parsed.revenue,
        eps=parsed.eps,
        net_profit=parsed.net_profit,
        currency=parsed.currency.strip() or None,
        period_label=parsed.period_label.strip() or None,
        sources=sources,
        notes=parsed.notes.strip() or None,
        **meta,
    )


@dataclass(slots=True)
class EarningsEnricher:
    """Wraps ``extract_from_url`` (+ optional ``find_consensus``) with a
    per-run budget so a burst of earnings filings cannot blow the LLM
    cost ceiling. One instance per catalyst_sync invocation, passed down
    to _process_news_item. Budget counts *reports*, not LLM calls — each
    report may trigger one extraction call and one consensus call."""

    api_key: str
    model: str
    budget: int
    max_chars: int
    fetch_timeout: int
    max_output_tokens: int
    consensus_enabled: bool = True
    consensus_timeout: int = 180
    consensus_max_output_tokens: int = 6000

    @property
    def configured(self) -> bool:
        return bool(self.api_key) and self.budget > 0

    async def maybe_extract(
        self, *, url: str | None, company_name: str, symbol: str
    ) -> EnrichmentResult | None:
        if not url or self.budget <= 0 or not self.api_key:
            return None
        result = await extract_from_url(
            url,
            company_name=company_name,
            symbol=symbol,
            api_key=self.api_key,
            model=self.model,
            max_chars=self.max_chars,
            fetch_timeout=self.fetch_timeout,
            max_output_tokens=self.max_output_tokens,
        )
        # Burn budget only when we actually reached (or definitively failed at)
        # the document. Transient fetch / LLM-HTTP errors can be retried next
        # run for free; otherwise a flaky upstream eats the whole run's budget.
        err = result.error or ""
        is_transient = result.status == "failed" and err.startswith(
            ("fetch_failed:", "llm_http_error:")
        )
        if not is_transient:
            self.budget -= 1
        if result.status != "completed":
            log.warning(
                "earnings_enrich_failed",
                url=url,
                symbol=symbol,
                error=result.error,
                fetched_bytes=result.fetched_bytes,
            )
            return result

        # Consensus pass: only after a successful extraction, only when we
        # have a period_label to ground the search. Failure here is silent —
        # the alert renders fine without consensus.
        period_label = (
            (result.financials or {}).get("period_label") if result.financials else None
        )
        if self.consensus_enabled and period_label:
            cr = await find_consensus(
                company_name=company_name,
                symbol=symbol,
                period_label=period_label,
                api_key=self.api_key,
                model=self.model,
                timeout_seconds=self.consensus_timeout,
                max_output_tokens=self.consensus_max_output_tokens,
            )
            if cr.status == "completed" and result.financials is not None:
                result.financials["consensus"] = {
                    "revenue": cr.revenue,
                    "eps": cr.eps,
                    "net_profit": cr.net_profit,
                    "currency": cr.currency,
                    "period_label": cr.period_label,
                    "sources": cr.sources,
                    "notes": cr.notes,
                }
            elif cr.status == "failed":
                log.warning(
                    "earnings_consensus_failed",
                    symbol=symbol,
                    period_label=period_label,
                    error=cr.error,
                )
        return result
