"""Earnings-report enrichment wiring for the catalyst pipeline (split).

The heavy lifting (URL fetch + structured-financials LLM extraction)
lives in ``earnings_enrich.EarningsEnricher``; this module owns the
catalyst-pipeline-side dispatch: building the per-run enricher from
runtime config and gating the per-item call on the classified subtype.

The other enrichment domains (EDGAR / HK IPO / CN IPO) have their own
``*_enrich`` services dispatched from ``tasks.py`` and were never part
of ``catalyst_sync``.
"""

from typing import Any

from catalyst_radar.config import settings
from catalyst_radar.services.earnings_enrich import EarningsEnricher

# Classified subtypes that qualify for structured-financials enrichment.
# Kept in lockstep with the classifier's event_subtype enum (schemas/llm.py).
_ENRICH_SUBTYPES = {"earnings_report", "guidance"}


def build_earnings_enricher(cfg) -> EarningsEnricher | None:
    """Construct the per-run earnings enricher from effective runtime
    config, or None when the feature is off / OpenAI is not configured.
    One instance per ``sync_catalysts`` invocation — it owns the per-run
    enrichment budget."""
    if not (bool(cfg.catalyst_enrich_earnings_enabled) and settings.openai_api_key):
        return None
    return EarningsEnricher(
        api_key=settings.openai_api_key,
        model=settings.catalyst_enrich_model,
        budget=int(cfg.catalyst_enrich_max_per_run),
        max_chars=settings.catalyst_enrich_max_chars,
        fetch_timeout=settings.catalyst_enrich_fetch_timeout_seconds,
        max_output_tokens=settings.catalyst_enrich_max_output_tokens,
        consensus_enabled=bool(cfg.catalyst_enrich_consensus_enabled),
        consensus_timeout=settings.catalyst_enrich_consensus_timeout_seconds,
        consensus_max_output_tokens=settings.catalyst_enrich_consensus_max_output_tokens,
    )


async def maybe_enrich_earnings(
    enricher: EarningsEnricher | None,
    *,
    subtype: str,
    url: str | None,
    company_name: str,
    symbol: str,
) -> dict[str, Any] | None:
    """Earnings-report enrichment: fetch the cited URL, extract financials
    via a 2nd LLM call. Returns the structured financials, or None when the
    item doesn't qualify or extraction yielded nothing. Best-effort only —
    a failure here must NEVER block the notification (the classifier already
    validated the item is company-critical; the user still wants the alert
    even if the PDF is unreadable)."""
    if enricher is None or subtype not in _ENRICH_SUBTYPES:
        return None
    result = await enricher.maybe_extract(url=url, company_name=company_name, symbol=symbol)
    if result is not None and result.financials is not None:
        return result.financials
    return None
