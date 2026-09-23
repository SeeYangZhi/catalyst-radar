"""Source discovery (Phase D).

Given a tracked company, find its canonical primary-source feeds — IR press
page, product/engineering blog, or (preferred) an RSS/Atom feed — and persist
them to ``company_sources``. The LLM call is open-ended (needs web search +
URL reasoning), so it belongs to an agent; it runs once per company, not on
the per-cycle hot path.

Discovery runs behind ``SourceDiscoveryProvider`` (currently OpenAI's
Responses ``web_search`` tool) so the rest of the pipeline doesn't care how
sources are found.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from openai import APIError, AsyncOpenAI

from catalyst_radar.config import settings
from catalyst_radar.logging import get_logger
from catalyst_radar.models.company import TrackedCompany
from catalyst_radar.prompt_context import current_date_line
from catalyst_radar.repositories.company_source_repository import CompanySourceRepository
from catalyst_radar.schemas.llm import SourceDiscoveryOutput
from catalyst_radar.services.openai_classifier import reasoning_off_for

log = get_logger(__name__)

# Source kinds discovery is allowed to emit; anything else is dropped.
ALLOWED_KINDS = {"rss", "ir_press", "blog", "sec", "hkex", "twitter"}

_SYSTEM = (
    "You find the official primary-source news channels for a public company "
    "so a catalyst monitor can watch them directly. Strongly PREFER machine-"
    "readable feeds: return an RSS or Atom feed URL whenever one exists for "
    "the company's investor-relations press releases or its product/engineering "
    "blog. Only return a plain HTML page URL when no feed is available. "
    "Return the company's OWN domains (its IR site, its blog) — not aggregators, "
    "not Yahoo/Google Finance, not news wires. Verify each URL looks like a real, "
    "canonical company page. "
    "Whenever you reference a stock ticker in the label or reasoning fields, "
    "prefix it with a dollar sign — e.g. $AAPL, $0700 — never write the bare "
    "symbol. "
    "Pick the closest matching kind from the enum. Prefer at most one IR-press "
    "source and one blog source. If you cannot find a credible source, return "
    "an empty sources list."
)



@dataclass(slots=True)
class DiscoveredSource:
    kind: str
    url: str
    label: str | None = None
    confidence: float | None = None
    reasoning: str | None = None
    verified: bool | None = None


@dataclass(slots=True)
class DiscoveryResult:
    status: str  # completed | failed
    sources: list[DiscoveredSource]
    provider: str
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class DiscoverySummary:
    status: str
    provider: str
    discovered: int = 0
    created: int = 0
    error: str | None = None


def _coerce_sources(parsed: dict[str, Any]) -> list[DiscoveredSource]:
    out: list[DiscoveredSource] = []
    for raw in parsed.get("sources", []) or []:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("kind", "")).strip().lower()
        url = str(raw.get("url", "")).strip()
        if kind not in ALLOWED_KINDS or not url.startswith(("http://", "https://")):
            continue
        confidence = raw.get("confidence")
        verified = raw.get("verified")
        out.append(
            DiscoveredSource(
                kind=kind,
                url=url,
                label=(str(raw["label"]).strip() if raw.get("label") else None),
                confidence=float(confidence) if isinstance(confidence, int | float) else None,
                reasoning=(str(raw["reasoning"]).strip() if raw.get("reasoning") else None),
                verified=bool(verified) if isinstance(verified, bool) else None,
            )
        )
    return out


class SourceDiscoveryProvider(ABC):
    name: str

    @property
    @abstractmethod
    def configured(self) -> bool: ...

    @abstractmethod
    async def discover(
        self, *, company_name: str, symbol: str, exchange: str
    ) -> DiscoveryResult: ...


class OpenAIWebSearchProvider(SourceDiscoveryProvider):
    """Discovery via OpenAI Responses API with the built-in ``web_search`` tool."""

    name = "openai"

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key if api_key is not None else settings.openai_api_key
        self.model = settings.catalyst_discovery_model
        self.timeout = settings.openai_timeout_seconds

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    async def discover(
        self, *, company_name: str, symbol: str, exchange: str
    ) -> DiscoveryResult:
        if not self.api_key:
            return DiscoveryResult("failed", [], self.name, error="OPENAI_API_KEY not configured")

        ticker = f"${symbol}" if symbol and not symbol.startswith("$") else (symbol or "")
        user = (
            f"Company: {company_name}\nTicker: {ticker} (exchange: {exchange})\n"
            "Find its official IR press-release feed/page and its product or "
            "engineering blog feed/page. Prefer RSS/Atom."
        )
        client = AsyncOpenAI(api_key=self.api_key, timeout=self.timeout)
        try:
            response = await client.responses.parse(
                model=self.model,
                input=[
                    {"role": "system", "content": _SYSTEM + current_date_line()},
                    {"role": "user", "content": user},
                ],
                text_format=SourceDiscoveryOutput,
                tools=[{"type": "web_search"}],
                max_output_tokens=settings.catalyst_discovery_max_output_tokens,
                reasoning=reasoning_off_for(self.model),
            )
        except APIError as exc:
            return DiscoveryResult("failed", [], self.name, error=repr(exc))
        finally:
            # Deterministic close — the SDK's __del__ otherwise schedules
            # aclose() on a later Celery task's loop (log noise on teardown).
            await client.close()

        usage = response.usage
        meta = {
            "model": response.model,
            "input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
        }
        if response.status != "completed" or response.output_parsed is None:
            return DiscoveryResult(
                "failed",
                [],
                self.name,
                error=str(response.incomplete_details or response.status),
                **meta,
            )
        # _coerce_sources takes a dict, so we hand it the model dump.
        return DiscoveryResult(
            "completed",
            _coerce_sources(response.output_parsed.model_dump()),
            self.name,
            **meta,
        )


def get_discovery_provider() -> SourceDiscoveryProvider:
    return OpenAIWebSearchProvider()


async def discover_sources_for_company(
    session,  # AsyncSession
    company: TrackedCompany,
    *,
    provider: SourceDiscoveryProvider | None = None,
) -> DiscoverySummary:
    """Run discovery for one company and persist any new sources.

    Existing source rows for the company are never overwritten, so a manual
    edit or a deliberate disable survives re-discovery. New rows are flagged
    ``needs_review=True`` so the UI can surface them for a quick human glance.
    """
    prov = provider or get_discovery_provider()
    if not prov.configured:
        return DiscoverySummary("failed", prov.name, error=f"{prov.name} provider not configured")

    result = await prov.discover(
        company_name=company.company_name, symbol=company.symbol, exchange=company.exchange
    )
    if result.status != "completed":
        log.warning(
            "source_discovery_failed",
            tracked_company_id=company.id,
            provider=prov.name,
            error=result.error,
        )
        return DiscoverySummary("failed", prov.name, error=result.error)

    repo = CompanySourceRepository(session)
    created = 0
    for src in result.sources:
        _record, was_created = await repo.upsert_discovered(
            tracked_company_id=company.id,
            kind=src.kind,
            url=src.url,
            label=src.label,
            source=f"discovery_{prov.name}",
            # An agent that actually loaded the URL doesn't need a human glance.
            needs_review=not bool(src.verified),
            discovery_payload={
                "confidence": src.confidence,
                "reasoning": src.reasoning,
                "verified": src.verified,
                "model": result.model,
            },
        )
        if was_created:
            created += 1
    await session.commit()

    log.info(
        "source_discovery_completed",
        tracked_company_id=company.id,
        provider=prov.name,
        discovered=len(result.sources),
        created=created,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
    )
    return DiscoverySummary(
        "completed", prov.name, discovered=len(result.sources), created=created
    )
