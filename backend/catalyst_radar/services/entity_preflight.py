"""Preflight: when the user adds a tracked company, ask the LLM to
resolve the issuer (entity), generate a short business summary, and
enumerate related entities (parents / JV partners / major shareholders)
that should be candidates for catalyst spillover propagation.

The output isn't applied directly — it lands in
``relationship_suggestions`` as a single ``pending`` row keyed to the
new tracked company. The user reviews + accepts each suggested
relationship on the settings page; only on accept do we upsert the
referenced entity, auto-track its primary listing as
``is_parent_only=True`` if not already tracked, and insert the
``entity_relationships`` edge.

This service is the *only* place that calls the preflight LLM. Both the
add-company endpoint and ``scripts/backfill_entities`` reuse it.
"""

from dataclasses import dataclass, field

from openai import APIError, AsyncOpenAI

from catalyst_radar.config import settings
from catalyst_radar.logging import get_logger
from catalyst_radar.prompt_context import current_date_line
from catalyst_radar.schemas.llm import EntityPreflightOutput
from catalyst_radar.services.openai_classifier import (
    OpenAIConfigError,
    reasoning_off_for,
)
from catalyst_radar.services.url_liveness import filter_live_sources

log = get_logger(__name__)


_SYSTEM = (
    "You resolve a public company to its underlying issuer (entity) and "
    "identify the related entities whose news could materially move the "
    "company's price. Use reputable sources only (the company's IR site, "
    "official exchange filings — HKEXnews, SEC EDGAR, CNINFO 巨潮资讯网 — "
    "Reuters, Bloomberg, FT, SCMP, Caixin, established trade press). "
    "\n\n"
    "Return:\n"
    "- entity: the issuer the input ticker belongs to. canonical_name is "
    "the standard English name (Hon Hai Precision Industry, not Foxconn). "
    "country is the ISO-2 code of the issuer's primary domicile (TW, CN, "
    "HK, US, JP, KR). ticker + exchange identify the primary listing — "
    "echo back the input. summary is 2-4 plain ENGLISH sentences "
    "(<= ~600 chars) describing what the company actually does, its "
    "products/customers and how it makes money. Translate Chinese / "
    "Japanese / Korean terms into standard English. Whenever you mention "
    "a ticker, prefix with $.\n"
    "- parents: entities that control the issuer (>= 50% holder, ultimate "
    "parent in a corporate group). Up to 3, ranked by control strength.\n"
    "- joint_venture_partners: entities that share material ownership of "
    "a JV with the issuer, OR are the other party in a strategic JV the "
    "issuer is part of. Up to 5.\n"
    "- major_shareholders: ONLY strategic / activist / controlling holders "
    "whose own corporate news materially moves the issuer's stock. Up to "
    "5. **HARD EXCLUDES** (never include even if their stake is >= 5%):\n"
    "  • any holder whose latest filing on this issuer is a Schedule 13G "
    "(passive disclosure) — they have explicitly disclaimed any intent "
    "to influence management, and their own news does not move the "
    "issuer;\n"
    "  • index-fund and passive-asset-manager families: Vanguard, "
    "BlackRock, State Street / SSGA, Fidelity / FMR, T. Rowe Price, "
    "Capital Group / Capital Research / Capital World, Geode, Northern "
    "Trust, JPMorgan Asset Management, Wellington, Schwab, "
    "Dimensional / DFA, Invesco, Eaton Vance, sovereign-wealth funds "
    "holding passive stakes;\n"
    "  • broker-dealer custodial positions (Citigroup C/O, BofA, "
    "Morgan Stanley wealth) — these are aggregations across clients, "
    "not strategic positions.\n"
    "  Acceptable inclusions are: an industrial parent / corporate "
    "minority strategic holder (e.g. Microsoft's OpenAI stake, "
    "Berkshire Hathaway equity positions, a Japanese keiretsu cross-"
    "holding), a publicly-named activist investor that has filed 13D, "
    "or a founder / insider holding through a publicly-traded vehicle. "
    "If no holder meets that bar, return an empty list — empty is the "
    "correct answer for most companies. Do NOT pad the list to reach "
    "any minimum.\n"
    "- sources: up to 5 URLs you actually consulted, most authoritative "
    "first.\n"
    "- notes: one short paragraph explaining the corporate-group context "
    "or flagging anything uncertain (e.g. 'recent restructuring; "
    "parent designation may change').\n"
    "\n"
    "If a related entity is private / unlisted, still include it with "
    "ticker='' and exchange='' — the user may want to track its impact "
    "through public proxies. If you cannot identify the issuer with "
    "confidence > 0.5, return entity.confidence < 0.5 and empty lists "
    "for parents/joint_venture_partners/major_shareholders rather than "
    "guessing."
)


@dataclass(slots=True)
class PreflightResult:
    """Wraps the EntityPreflightOutput plus call metadata. ``status`` is
    ``completed`` on success, ``failed`` on API/parse errors. On failure
    the caller persists nothing and surfaces an error to the user — they
    can re-run preflight from the settings page later."""

    status: str
    output: EntityPreflightOutput | None = None
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    error: str | None = None
    sources: list[dict[str, str]] = field(default_factory=list)


class EntityPreflight:
    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key if api_key is not None else settings.openai_api_key
        self.model = settings.relationship_preflight_model
        # web_search latency runs 30-90s; reuse the consensus timeout
        # ceiling — same hosted-tool class.
        self.timeout = settings.catalyst_enrich_consensus_timeout_seconds
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

    async def preflight(
        self,
        *,
        company_name: str,
        symbol: str,
        exchange: str,
        country: str | None = None,
    ) -> PreflightResult:
        client = self._require_client()
        ticker = f"${symbol}" if symbol and not symbol.startswith("$") else (symbol or "")
        loc_bits = [f"Ticker: {ticker}", f"Exchange: {exchange}"]
        if country:
            loc_bits.append(f"Country: {country}")
        user = (
            f"Company: {company_name}\n"
            + "\n".join(loc_bits)
            + "\n\nResolve the issuer and enumerate related entities for "
            "catalyst spillover."
        )
        try:
            response = await client.responses.parse(
                model=self.model,
                input=[
                    {"role": "system", "content": _SYSTEM + current_date_line()},
                    {"role": "user", "content": user},
                ],
                text_format=EntityPreflightOutput,
                tools=[{"type": "web_search"}],
                max_output_tokens=settings.catalyst_enrich_consensus_max_output_tokens,
                reasoning=reasoning_off_for(self.model),
            )
        except APIError as exc:
            return PreflightResult("failed", error=repr(exc))

        usage = response.usage
        meta = {
            "model": response.model,
            "input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
        }
        if response.status != "completed" or response.output_parsed is None:
            return PreflightResult(
                "failed",
                error=str(response.incomplete_details or response.status),
                **meta,
            )

        parsed = response.output_parsed
        sources = [
            {"name": s.name, "url": s.url}
            for s in parsed.sources
            if s.name.strip() and s.url.strip()
        ]
        # Strip dead/hallucinated URLs so the relationship-suggestion UI
        # doesn't surface citations the user cannot open.
        sources = await filter_live_sources(sources)
        return PreflightResult(
            "completed", output=parsed, sources=sources, **meta
        )
