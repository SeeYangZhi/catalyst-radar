"""Turn a prospectus-summary slice into a one-line business description
(+ market cap when stated) via the OpenAI Responses API.

Two paths:
- ``summarize()`` — fast filing-derived path: takes a prospectus excerpt
  (S-1/F-1/424B for US, HKEXnews PDF for HK) and asks the LLM to write
  the description from that text.
- ``describe_via_web()`` — fallback when the filing path returns an
  empty description (table-of-contents page, image-only PDF, etc.). Uses
  OpenAI Responses + the hosted ``web_search`` tool against reputable
  sources (HKEXnews, IR sites, Reuters/Bloomberg/FT/SCMP/Caixin) and
  cites the URLs it used so the alert can show provenance.
"""

from dataclasses import dataclass, field

from openai import APIError, AsyncOpenAI

from catalyst_radar.config import settings
from catalyst_radar.logging import get_logger
from catalyst_radar.prompt_context import current_date_line
from catalyst_radar.schemas.llm import IpoSummaryOutput, IpoWebDescriptionOutput
from catalyst_radar.services.openai_classifier import (
    OpenAIConfigError,
    reasoning_off_for,
)
from catalyst_radar.services.url_liveness import filter_live_sources

log = get_logger(__name__)

_SYSTEM = (
    "You summarize IPO prospectuses for a trader's watchlist. Given an "
    "excerpt from a registration statement (S-1/F-1/424B for US, HKEXnews "
    "for HK, 招股说明书 for mainland China), produce: "
    "description — 2-4 plain sentences in ENGLISH, up to ~600 chars, what "
    "the company actually does, its products/customers and how it makes "
    "money; no marketing language, no 'we believe'; do not truncate "
    "mid-sentence. **Always write the description in English even when the "
    "source excerpt is Chinese** — translate Chinese product / customer / "
    "industry terms into their standard English equivalents (e.g. "
    "电池管理系统 → battery management system, 储能 → energy storage, "
    "锂电池 → lithium-ion battery). **The description MUST NOT contain "
    "any Chinese / Japanese / Korean character — not for product names, "
    "not for industry terms, and NOT for the company name itself.** If "
    "you need to refer to the company inside the description, use its "
    "filed English name (e.g. 'GreatLight Electronics' for 高特电子) or "
    "the standard pinyin transliteration (e.g. 'Weitongli' for 维通利) "
    "— never the original CJK form. The Event row will continue to "
    "carry the original CJK company_name as the card title; you are "
    "writing the body copy. Whenever you reference a stock ticker, "
    "prefix it with a dollar sign — e.g. $AAPL, $0700, $688635 — never "
    "write the bare symbol. If the excerpt is not a real business "
    "description, set description to an empty string. "
    "market_cap_usd — implied/indicative post-IPO equity market "
    "capitalization in USD if the text states or clearly implies it, else null."
)

_WEB_SYSTEM = (
    "You write a concise business description for a single public company "
    "so a trader's IPO alert is informative. "
    "description is 2-4 plain sentences in ENGLISH, up to ~600 chars: what "
    "the company actually does, its products/customers, how it makes money "
    "— no marketing language, no 'we believe', no 'leading'. Do not "
    "truncate mid-sentence. **Always write in English even when the most "
    "authoritative sources are in Chinese** — translate domain terms to "
    "their standard English equivalents. **The description MUST NOT "
    "contain any Chinese / Japanese / Korean character — not for product "
    "names, not for industry terms, and NOT for the company name "
    "itself.** Use the filed English name or standard pinyin "
    "transliteration when you need to refer to the company. Whenever "
    "you reference a stock ticker, prefix it with a dollar sign — e.g. "
    "$AAPL, $0700, $688635 — never write the bare symbol. "
    "Use reputable sources only: the company's IR site, the exchange's "
    "filings (HKEXnews, SEC EDGAR, CNINFO 巨潮资讯网), Reuters, Bloomberg, "
    "FT, SCMP, Caixin, established trade press. Up to 3 sources, most-"
    "authoritative first. If you cannot find reliable information from "
    "reputable sources, return an empty description and an empty sources "
    "list — do NOT invent or guess."
)


@dataclass(slots=True)
class IpoSummary:
    description: str | None
    market_cap_usd: float | None
    model: str | None = None
    error: str | None = None


@dataclass(slots=True)
class IpoWebDescription:
    """Result of a web_search description lookup. `description` may be
    empty if no reputable sources were found — that's the correct outcome
    for ultra-thinly-covered names, not an error."""

    status: str  # completed | failed
    description: str | None = None
    sources: list[dict[str, str]] = field(default_factory=list)
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    error: str | None = None


class IpoSummarizer:
    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key if api_key is not None else settings.openai_api_key
        self.model = settings.catalyst_classifier_model
        self.timeout = settings.openai_timeout_seconds
        self._client = (
            AsyncOpenAI(api_key=self.api_key, timeout=self.timeout)
            if self.api_key
            else None
        )

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    async def aclose(self) -> None:
        """Deterministic close — see OpenAIClassifier.aclose for why
        (GC-scheduled aclose on a later task's loop under Celery)."""
        if self._client is not None:
            await self._client.close()
            self._client = None

    def _require_client(self) -> AsyncOpenAI:
        if self._client is None:
            raise OpenAIConfigError("OPENAI_API_KEY is not configured")
        return self._client

    async def summarize(self, *, company_name: str, symbol: str, summary_text: str) -> IpoSummary:
        client = self._require_client()
        ticker = f"${symbol}" if symbol and not symbol.startswith("$") else (symbol or "")
        user = f"Company: {company_name} ({ticker})\nProspectus excerpt:\n{summary_text[:12000]}"
        try:
            response = await client.responses.parse(
                model=self.model,
                input=[
                    {"role": "system", "content": _SYSTEM + current_date_line()},
                    {"role": "user", "content": user},
                ],
                text_format=IpoSummaryOutput,
                temperature=0,
                max_output_tokens=400,
                reasoning=reasoning_off_for(self.model),
            )
        except APIError as exc:
            return IpoSummary(None, None, error=repr(exc))

        if response.status != "completed" or response.output_parsed is None:
            return IpoSummary(
                None,
                None,
                model=response.model,
                error=str(response.incomplete_details or response.status),
            )

        parsed = response.output_parsed
        desc = parsed.description.strip() or None
        return IpoSummary(desc, parsed.market_cap_usd, model=response.model)

    async def describe_via_web(
        self,
        *,
        company_name: str,
        symbol: str,
        exchange: str | None = None,
        listing_date: str | None = None,
    ) -> IpoWebDescription:
        """Fallback when the prospectus path couldn't produce a description
        (table-of-contents page, image-only PDF, parse failure). Uses
        OpenAI Responses + web_search against reputable sources and
        captures source URLs so the alert can show provenance.

        Always returns a result — never raises. `status='completed'` with
        empty `description`/`sources` is the correct outcome when no
        reputable coverage exists (ultra-thinly-covered name)."""
        if self._client is None:
            return IpoWebDescription("failed", error="OPENAI_API_KEY not configured")

        ticker = f"${symbol}" if symbol and not symbol.startswith("$") else (symbol or "")
        loc_bits: list[str] = []
        if ticker:
            loc_bits.append(f"Ticker: {ticker}")
        if exchange:
            loc_bits.append(f"Exchange: {exchange}")
        if listing_date:
            loc_bits.append(f"Listing date: {listing_date}")
        ctx = "\n".join(loc_bits)
        user = (
            f"Company: {company_name}\n{ctx}\n\n"
            "Find a concise business description for this company."
        )
        # web_search runs 30-90s; the consensus timeout is the right ceiling.
        client = self._client.with_options(
            timeout=settings.catalyst_enrich_consensus_timeout_seconds
        )
        try:
            response = await client.responses.parse(
                model=settings.catalyst_enrich_model,
                input=[
                    {"role": "system", "content": _WEB_SYSTEM + current_date_line()},
                    {"role": "user", "content": user},
                ],
                text_format=IpoWebDescriptionOutput,
                tools=[{"type": "web_search"}],
                max_output_tokens=settings.catalyst_enrich_consensus_max_output_tokens,
                reasoning=reasoning_off_for(settings.catalyst_enrich_model),
            )
        except APIError as exc:
            return IpoWebDescription("failed", error=repr(exc))

        usage = response.usage
        meta = {
            "model": response.model,
            "input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
        }
        if response.status != "completed" or response.output_parsed is None:
            return IpoWebDescription(
                "failed",
                error=str(response.incomplete_details or response.status),
                **meta,
            )

        parsed = response.output_parsed
        desc = parsed.description.strip() or None
        sources = [
            {"name": s.name, "url": s.url}
            for s in parsed.sources[:3]
            if s.name.strip() and s.url.strip()
        ]
        # Drop dead-link citations so the alert doesn't surface fabricated
        # URLs. The description body stays — only unreachable links go.
        sources = await filter_live_sources(sources)
        return IpoWebDescription("completed", description=desc, sources=sources, **meta)
