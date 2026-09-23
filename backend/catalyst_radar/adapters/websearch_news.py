"""Catalyst news via OpenAI web_search (EODHD-gap markets).

For markets EODHD doesn't cover (e.g. Taiwan), there is no news feed to crawl.
This adapter asks the OpenAI Responses API — with the hosted ``web_search``
tool — for recent material catalysts for one company and returns items in the
same normalized shape the catalyst pipeline already consumes
(``{title, content, link, date}``), so they flow through the identical
prefilter → classifier → event path.

Scoped to gap markets so there is no overlap with EODHD and therefore no
cross-source double-send (Phase C dedup is not required yet for this path).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from openai import APIError, AsyncOpenAI

from catalyst_radar.config import settings
from catalyst_radar.dedup import (
    dedup_key as make_dedup_key,
)
from catalyst_radar.dedup import (
    normalize_url,
    stable_hash,
)
from catalyst_radar.dedup import (
    source_event_id as make_source_event_id,
)
from catalyst_radar.prompt_context import current_date_line
from catalyst_radar.schemas.llm import WebSearchNewsOutput
from catalyst_radar.services.openai_classifier import reasoning_off_for
from catalyst_radar.services.url_liveness import ProbeOutcome, probe_urls

_SYSTEM = (
    "You find recent material catalysts and company-specific news for a single "
    "public company so a trader can be alerted. Return ONLY genuine, potentially "
    "stock-moving items: product launches, M&A, major partnerships/customers, "
    "regulatory decisions, guidance changes, major financing/issuance, "
    "management changes, uplisting, or major operational news. Exclude routine "
    "marketing, ESG/community content, minor awards, and articles that only "
    "mention the company in passing. Use the company's primary/reputable sources "
    "and return the canonical article URL (not an aggregator search page). "
    "published_date is YYYY-MM-DD or null if the source does not state it. "
    "If there is no material news, return an empty items list."
)

@dataclass(slots=True)
class WebSearchResult:
    status: str  # completed | failed
    items: list[dict[str, Any]]  # normalized: {title, content, link, date}
    source_url: str | None = None
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    # Per-URL liveness-probe outcomes . catalyst_sync aggregates
    # these into the source-run summary so probe telemetry survives deploys.
    url_probes: list[ProbeOutcome] = field(default_factory=list)


def _normalize(item: Any) -> dict[str, Any] | None:
    """Reject items whose URL is not a real http(s) link — the strict
    schema guarantees the shape but cannot reject "http://example" style
    placeholders the model occasionally invents."""
    title = item.title.strip()
    url = item.url.strip()
    if not title or not url.startswith(("http://", "https://")):
        return None
    return {
        "title": title,
        "content": item.summary.strip(),
        "link": url,
        "date": item.published_date,
    }


class WebSearchNewsAdapter:
    source_name = "websearch_news"
    schema_name = "openai.websearch.v1"

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key if api_key is not None else settings.openai_api_key
        self.model = settings.catalyst_websearch_model
        self.timeout = settings.openai_timeout_seconds
        self.lookback_days = settings.catalyst_websearch_lookback_days
        self.max_items = settings.catalyst_websearch_max_items

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def source_event_id(self, item: dict[str, Any]) -> str:
        # Hash on the canonical URL (lowercased host, no /mobile/ or /amp/,
        # tracking params stripped) so mobile/desktop variants of the same
        # page collide into one sid and the second emission short-circuits
        # at events.get_by_source_event_id. Fall back to title+date (not
        # title alone) so distinct link-less stories with the same headline
        # don't collapse.
        link = item.get("link")
        basis = normalize_url(link) if link else f"{item.get('title', '')}|{item.get('date', '')}"
        return make_source_event_id("websearch", "news", stable_hash(basis))

    def dedup_key(self, item: dict[str, Any]) -> str:
        link = item.get("link")
        return make_dedup_key(normalize_url(link) if link else item.get("title"))

    async def fetch(self, *, company_name: str, symbol: str, exchange: str) -> WebSearchResult:
        if not self.api_key:
            return WebSearchResult("failed", [], error="OPENAI_API_KEY not configured")

        user = (
            f"Company: {company_name}\nTicker: {symbol} (exchange: {exchange})\n"
            f"Find material catalysts/news from roughly the last {self.lookback_days} days. "
            f"Return at most {self.max_items} items, most recent first."
        )
        client = AsyncOpenAI(api_key=self.api_key, timeout=self.timeout)
        try:
            response = await client.responses.parse(
                model=self.model,
                input=[
                    {"role": "system", "content": _SYSTEM + current_date_line()},
                    {"role": "user", "content": user},
                ],
                text_format=WebSearchNewsOutput,
                tools=[{"type": "web_search"}],
                max_output_tokens=settings.catalyst_discovery_max_output_tokens,
                reasoning=reasoning_off_for(self.model),
            )
        except APIError as exc:
            return WebSearchResult("failed", [], error=repr(exc))
        finally:
            # Deterministic close: the SDK's __del__ otherwise schedules
            # aclose() on whatever loop runs at GC time (a later Celery
            # task's loop → 'Task exception was never retrieved' noise).
            await client.close()

        usage = response.usage
        meta = {
            "model": response.model,
            "input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
        }
        if response.status != "completed" or response.output_parsed is None:
            return WebSearchResult(
                "failed",
                [],
                error=str(response.incomplete_details or response.status),
                **meta,
            )

        items = [n for n in (_normalize(it) for it in response.output_parsed.items) if n]
        items, probes = await self._apply_liveness(items)
        return WebSearchResult("completed", items[: self.max_items], url_probes=probes, **meta)

    async def _apply_liveness(
        self, items: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[ProbeOutcome]]:
        """web_search occasionally hallucinates URLs that follow a site's
        template (e.g. an IR site's `FinanceReport/{Y}Q{N}EN.pdf`). Drop
        items whose link 404s before they become a catalyst with a dead
        source_url — a citation the user cannot verify is worse than no
        item at all. Returns the surviving items plus the per-URL probe
        outcomes for source-run telemetry ."""
        if not items:
            return items, []
        outcomes = await probe_urls([it["link"] for it in items])
        live = {o.url for o in outcomes if o.kept}
        return [it for it in items if it.get("link") in live], outcomes
