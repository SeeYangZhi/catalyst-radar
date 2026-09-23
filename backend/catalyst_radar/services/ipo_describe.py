"""Two-tier IPO description policy, shared by the US/HK/CN enrichers.

Tier 1 (provisional): for imminent events (about to alert) run OpenAI
web_search first — fast, no prospectus dependency — so the FIRST alert
carries a blurb. Tier 2 (authoritative): a later run fetches the canonical
prospectus/EDGAR filing and UPGRADES the provisional in place (alert_backfill
edits the already-sent message). A boilerplate guard stops a poorly-extracted
filing from overwriting a good web blurb. Backlog events stay prospectus-first.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from catalyst_radar.logging import get_logger
from catalyst_radar.models.event import Event
from catalyst_radar.services.ipo_summarizer import IpoSummarizer

log = get_logger(__name__)

# A market-specific prospectus fetcher: returns a dict carrying at least
# ``summary`` (text to summarize) plus optional metadata keys we persist
# (filing_url, doc_type, form, filing_date), or None when no filing is
# available yet.
FetchProspectus = Callable[[Event], Awaitable[dict | None]]

_META_KEYS = ("filing_url", "doc_type", "form", "filing_date")

# Phrases that mark prospectus text we accidentally scraped from the
# subscription / application procedure pages rather than the business summary
# (observed: a HKSE "…describing the public offering process…" extraction).
_BOILERPLATE_MARKERS = (
    "public offering process",
    "application process",
    "application channel",
    "subscription procedure",
    "how to apply",
    "offering process",
)


def _is_boilerplate(text: str) -> bool:
    low = text.lower()
    return any(m in low for m in _BOILERPLATE_MARKERS)


@dataclass(slots=True)
class DescribeOutcome:
    described: bool  # a description was set or upgraded on this call
    errors: int


async def describe_ipo_event(
    event: Event,
    *,
    summarizer: IpoSummarizer,
    fetch_prospectus: FetchProspectus,
    exchange: str | None,
    imminent: bool,
    websearch_enabled: bool,
    prospectus_source: str,
) -> DescribeOutcome:
    """Apply the two-tier policy to one event, mutating ``event.payload``.
    Best-effort: every failure is logged and swallowed, never raised."""
    original = (event.payload or {}).get("profile") or {}
    already_attempted = bool(original.get("prospectus_attempted"))
    profile = dict(original)
    profile["checked"] = True
    errors = 0
    described = False
    web_ok = websearch_enabled and summarizer.configured

    async def _try_prospectus() -> bool:
        nonlocal errors
        if already_attempted:
            return False  # canonical fetch is once-only; web fallback still retries each run
        profile["prospectus_attempted"] = True
        try:
            found = await fetch_prospectus(event)
        except Exception as exc:  # noqa: BLE001 - one filing must not abort the run
            errors += 1
            log.warning("ipo_prospectus_fetch_failed", code=event.symbol, error=repr(exc))
            return False
        if not found:
            return False
        for k in _META_KEYS:
            if found.get(k):
                profile[k] = found[k]
        try:
            s = await summarizer.summarize(
                company_name=event.company_name or "",
                symbol=event.symbol or "",
                summary_text=found["summary"],
            )
        except Exception as exc:  # noqa: BLE001
            errors += 1
            log.warning("ipo_summarize_failed", code=event.symbol, error=repr(exc))
            return False
        if not (s and s.description):
            return False
        if profile.get("description") and _is_boilerplate(s.description):
            log.info("ipo_upgrade_rejected_boilerplate", code=event.symbol)
            return False
        profile["description"] = s.description
        profile["description_source"] = prospectus_source
        if s.market_cap_usd:
            profile["market_cap"] = s.market_cap_usd
        return True

    async def _try_websearch() -> bool:
        nonlocal errors
        if not web_ok:
            return False
        try:
            wd = await summarizer.describe_via_web(
                company_name=event.company_name or "",
                symbol=event.symbol or "",
                exchange=exchange,
                listing_date=(
                    event.event_date.date().isoformat() if event.event_date else None
                ),
            )
        except Exception as exc:  # noqa: BLE001
            errors += 1
            log.warning("ipo_websearch_failed", code=event.symbol, error=repr(exc))
            return False
        if not (wd and wd.description):
            return False
        profile["description"] = wd.description
        profile["description_source"] = "websearch"
        if wd.sources:
            profile["description_sources"] = wd.sources
        return True

    if original.get("description_source") == "websearch" and not already_attempted:
        # Tier 2: provisional already shown — try the canonical source once.
        described = await _try_prospectus()
    elif not profile.get("description"):
        if imminent and web_ok:
            # Tier 1: fast provisional first; prospectus if web found nothing.
            described = await _try_websearch() or await _try_prospectus()
        else:
            # Backlog: canonical-first, web fallback (unchanged behaviour).
            described = await _try_prospectus() or await _try_websearch()

    payload = dict(event.payload or {})
    payload["profile"] = profile
    event.payload = payload
    return DescribeOutcome(described=described, errors=errors)
