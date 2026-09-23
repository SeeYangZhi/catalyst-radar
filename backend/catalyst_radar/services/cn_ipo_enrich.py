"""Enrich CN IPO events with a CNINFO-prospectus business description.

Mirror of `hk_ipo_enrich` for SSE + SZSE: resolve the CNINFO prospectus
for each recent CN IPO lacking a profile, extract the business-chapter
slice (section-aware, A-share prospectuses are 200-800 pages), LLM-
summarize, and write `payload['profile']`. Idempotent — events marked
checked. CNINFO is rate-limited (1 req/s) and PDFs are large, so the
per-run budget is small.

Matching strategy: CNINFO returns ~60 most-recent A-share filings per
sync; we index those by `sec_code` and look up the candidate IPO event
by its 6-digit ticker. If no filing is found (the IPO was discovered by
akshare but CNINFO hasn't published the prospectus yet, or the
prospectus already aged out of the recent window), we fall back to the
web_search descriptor — same pattern as HK.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.adapters.cninfo_filings import CninfoFilingsAdapter
from catalyst_radar.logging import get_logger
from catalyst_radar.models.base import utcnow
from catalyst_radar.models.event import Event
from catalyst_radar.repositories.notification_repository import NotificationRepository
from catalyst_radar.repositories.source_repository import SourceRunRepository
from catalyst_radar.services.cn_prospectus_extract import extract_business_section
from catalyst_radar.services.ipo_summarizer import IpoSummarizer
from catalyst_radar.services.ipo_sync import select_enrich_candidates

log = get_logger(__name__)


# Doc types we will feed the LLM summarizer (in order of preference).
# 上市公告书 etc carry less business detail than the prospectuses but
# the indicative prospectus is a useful fallback when 招股说明书 hasn't
# been filed yet.
_PROSPECTUS_DOC_TYPES = ("prospectus", "prospectus_intent")


@dataclass(slots=True)
class CnIpoEnrichSummary:
    candidates: int
    enriched: int
    described: int
    errors: int


# Hard ceiling on how many prospectuses one run may enrich. Bounds the
# cold-start burst (a fresh DB backfills thousands of historical IPOs) so a
# single task can't run for hours; the rest drain over subsequent runs. Per
# item the cost is one bounded PDF download/parse plus a ~12k-char LLM call.
_IMMINENT_CAP = 40


def _max_alert_window_days(spec: str, default: int = 14) -> int:
    """Largest lead time (days) at which an IPO alert fires, from a
    "14,7,1"-style window spec. Items inside this window are about to be
    dispatched, so they must be enriched first."""
    try:
        return max(int(x) for x in spec.split(",") if x.strip())
    except ValueError:
        return default


def _pick_best_filing(filings: list[dict]) -> dict | None:
    """Prefer 招股说明书 (formal prospectus) over 招股意向书 (indicative).
    Within each tier prefer the most recent. Skip filings we cannot feed
    to the LLM summarizer."""
    relevant = [f for f in filings if f.get("doc_type") in _PROSPECTUS_DOC_TYPES]
    if not relevant:
        return None
    type_rank = {t: i for i, t in enumerate(_PROSPECTUS_DOC_TYPES)}
    relevant.sort(
        key=lambda f: (
            type_rank.get(f["doc_type"], 99),
            -(f["announcement_time"].timestamp() if f.get("announcement_time") else 0),
        )
    )
    return relevant[0]


async def enrich_cn_ipos(
    session: AsyncSession,
    adapter: CninfoFilingsAdapter | None = None,
    summarizer: IpoSummarizer | None = None,
) -> CnIpoEnrichSummary:
    from catalyst_radar.config import settings
    from catalyst_radar.runtime_config import effective

    eff = await effective(session)
    if not bool(eff.cn_ipo_prospectus_enrich_enabled):
        log.info("cn_ipo_enrich_disabled")
        return CnIpoEnrichSummary(0, 0, 0, 0)

    budget = int(settings.cn_ipo_prospectus_max_items_per_run)
    adapter = adapter or CninfoFilingsAdapter()
    summarizer = summarizer or IpoSummarizer()

    now = utcnow()
    stmt = (
        select(Event)
        .where(
            Event.event_type == "ipo",
            Event.country == "CN",
            Event.symbol.is_not(None),
            Event.event_date.is_not(None),
            Event.event_date >= now - timedelta(days=14),
            Event.event_date <= now + timedelta(days=120),
        )
        .order_by(Event.event_date)
        .limit(100)
    )
    rows = list((await session.execute(stmt)).scalars().all())

    # Enrich-before-dispatch: pending-notification events first (CSRC
    # review-stage rows alert on meeting dates up to 14d past / 90d future,
    # far outside the listing-window heuristic), with the event_date window
    # kept for rows whose notifications haven't been minted yet. See
    # ipo_sync.select_enrich_candidates.
    window_days = _max_alert_window_days(
        getattr(eff, "ipo_alert_windows_days", "") or ""
    )
    candidates = await select_enrich_candidates(
        session,
        rows,
        budget=budget,
        cap=_IMMINENT_CAP,
        now=now,
        window_end=now + timedelta(days=window_days),
    )

    # Gate: nothing to enrich → don't open a source_run at all. The candidate
    # query above is cheap; recording an empty (item_count=0) run every cycle
    # is what bloated source_runs (the dominant source by volume). A run row
    # now means real work happened. The watchdog only flags sources that
    # ran-and-failed, so an idle source recording nothing won't false-alarm.
    if not candidates:
        return CnIpoEnrichSummary(0, 0, 0, 0)

    runs = SourceRunRepository(session)
    run = await runs.start("cninfo.prospectus_enrich")

    # Single CNINFO pull covers all candidates — pull once, index by code.
    try:
        filings = await adapter.fetch_recent()
    except Exception as exc:  # noqa: BLE001
        log.warning("cninfo_fetch_failed", error=repr(exc))
        filings = []
    by_code: dict[str, list[dict]] = {}
    for f in filings:
        by_code.setdefault(f["sec_code"], []).append(f)

    # Precise 'about to be alerted' set: events whose notification is pending
    # dispatch get the fast web_search provisional FIRST (so the first alert
    # carries a blurb), instead of waiting on the slow CNINFO PDF.
    pending_ids = await NotificationRepository(session).pending_event_ids()

    from catalyst_radar.services.ipo_describe import describe_ipo_event

    # Auxiliary prospectus metadata (filing_title / prospectus_pages /
    # prospectus_section) the shared helper does not persist itself — _fetch
    # stashes it per event so we can merge it back after the helper call.
    aux: dict[int, dict] = {}

    async def _fetch(e: Event) -> dict | None:
        """Resolve, download and section-extract the CNINFO prospectus for one
        event, returning {"summary": <business section text>, ...filing meta}
        for the shared helper to summarize — or None when no filing/section is
        available (so the helper's web fallback takes over). Reuses the
        pre-built by_code index; never refetches the CNINFO listing."""
        filing = _pick_best_filing(by_code.get(e.symbol or "", []))
        if filing is None:
            return None
        extra: dict = {
            "filing_url": filing["pdf_url"],
            "doc_type": filing["doc_type"],
            "filing_title": filing["title"],
        }
        # Stash filing meta up front so it survives even a download/parse
        # failure (the helper swallows the raise and the web fallback runs).
        aux[e.id] = extra
        pdf_bytes = await adapter.download_pdf(filing["pdf_url"])
        if not pdf_bytes:
            return None
        section_text, meta = extract_business_section(pdf_bytes)
        extra["prospectus_pages"] = meta.get("pages_total")
        extra["prospectus_section"] = (
            f"pp.{meta['start_page']}-{meta['end_page']}"
            if meta.get("start_page")
            else "fallback_frontmatter"
        )
        aux[e.id] = extra
        if not section_text:
            return None
        return {"summary": section_text, **extra}

    enriched = described = errors = 0
    for event in candidates:
        # Seed CN-specific profile fields the shared helper does not set
        # (source/as_of/currency); the helper copies event.payload["profile"]
        # before writing description/meta, so these are preserved.
        existing_profile = (event.payload or {}).get("profile") or {}
        payload = dict(event.payload or {})
        payload["profile"] = {
            **existing_profile,
            "source": existing_profile.get("source", "cninfo"),
            "as_of": now.date().isoformat(),
            "currency": existing_profile.get("currency", "CNY"),
        }
        event.payload = payload

        outcome = await describe_ipo_event(
            event,
            summarizer=summarizer,
            fetch_prospectus=_fetch,
            exchange=event.exchange,
            imminent=event.id in pending_ids,
            websearch_enabled=bool(eff.ipo_websearch_provisional_enabled),
            prospectus_source="prospectus",
        )
        # Merge back the auxiliary prospectus metadata _fetch captured (filing
        # title + page/section markers) that the helper does not persist.
        extra = aux.get(event.id)
        if extra:
            merged = dict(event.payload or {})
            merged_profile = dict(merged.get("profile") or {})
            for k, v in extra.items():
                if v is not None:
                    merged_profile.setdefault(k, v)
            merged["profile"] = merged_profile
            event.payload = merged
        described += 1 if outcome.described else 0
        errors += outcome.errors
        session.add(event)
        enriched += 1

    await runs.finish(run, status="success", item_count=enriched)
    await NotificationRepository(session).release_dispatch_grace(
        [e.id for e in candidates if (e.payload or {}).get("profile", {}).get("description")]
    )
    await session.commit()

    # Self-heal alerts already sent before the description landed: edit the
    # existing Telegram message in place. Best-effort — never abort enrich.
    try:
        from catalyst_radar.services.alert_backfill import backfill_alert_edits

        await backfill_alert_edits(session, candidates)
    except Exception as exc:  # noqa: BLE001
        log.warning("cn_ipo_alert_backfill_failed", error=repr(exc))

    log.info(
        "cn_ipo_enrich_ok",
        candidates=len(candidates),
        enriched=enriched,
        described=described,
        errors=errors,
    )
    return CnIpoEnrichSummary(len(candidates), enriched, described, errors)
