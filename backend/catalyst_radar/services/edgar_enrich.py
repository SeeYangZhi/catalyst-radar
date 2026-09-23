"""Enrich US IPO events with a filings-based one-liner + market cap.

Phase 9.6a. For recent US IPO events lacking a profile, resolve the
SEC EDGAR registration filing, extract the prospectus-summary slice,
LLM-summarize it, and write ``payload['profile']``. Idempotent: an
event is skipped once its profile has been checked (so unmatched
companies are not re-queried every run).
"""

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.adapters.sec_edgar import SecEdgarAdapter
from catalyst_radar.logging import get_logger
from catalyst_radar.models.base import utcnow
from catalyst_radar.models.event import Event
from catalyst_radar.repositories.notification_repository import NotificationRepository
from catalyst_radar.repositories.source_repository import SourceRunRepository
from catalyst_radar.services.alerts import _deal_size
from catalyst_radar.services.ipo_summarizer import IpoSummarizer
from catalyst_radar.services.ipo_sync import select_enrich_candidates

log = get_logger(__name__)

_US = {"US", "NASDAQ", "NYSE", "NYSE AMERICAN", "NYSEARCA", "AMEX", "OTC"}


@dataclass(slots=True)
class EdgarEnrichSummary:
    candidates: int
    enriched: int
    described: int
    errors: int


async def enrich_us_ipos(
    session: AsyncSession,
    adapter: SecEdgarAdapter | None = None,
    summarizer: IpoSummarizer | None = None,
) -> EdgarEnrichSummary:
    from catalyst_radar.runtime_config import effective

    eff = await effective(session)
    if not bool(eff.sec_edgar_enrich_enabled):
        log.info("edgar_enrich_disabled")
        return EdgarEnrichSummary(0, 0, 0, 0)

    from catalyst_radar.config import settings

    budget = int(settings.sec_edgar_max_items_per_run)
    adapter = adapter or SecEdgarAdapter()
    summarizer = summarizer or IpoSummarizer()
    runs = SourceRunRepository(session)
    run = await runs.start("sec_edgar.enrich")

    now = utcnow()
    stmt = (
        select(Event)
        .where(
            Event.event_type == "ipo",
            Event.country == "US",
            Event.company_name.is_not(None),
            Event.event_date.is_not(None),
            Event.event_date >= now - timedelta(days=7),
            Event.event_date <= now + timedelta(days=120),
        )
        .order_by(Event.event_date)
        .limit(200)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    # Pending-notification events first (see ipo_sync.select_enrich_candidates).
    candidates = await select_enrich_candidates(session, rows, budget=budget)

    # Precise 'about to be alerted' set: events whose notification is pending
    # dispatch get the fast web_search provisional FIRST (so the first alert
    # carries a blurb), instead of waiting on the slow EDGAR filing fetch.
    pending_ids = await NotificationRepository(session).pending_event_ids()

    from catalyst_radar.services.ipo_describe import describe_ipo_event

    async def _fetch(e: Event) -> dict | None:
        return await adapter.fetch_summary(e.company_name or "")

    enriched = described = errors = 0
    for event in candidates:
        # Seed US-specific profile fields the shared helper does not set
        # (source/as_of/currency/deal_size); the helper copies
        # event.payload["profile"] before writing description/meta, so these
        # are preserved.
        existing_profile = (event.payload or {}).get("profile") or {}
        payload = dict(event.payload or {})
        seeded: dict = {
            **existing_profile,
            "source": existing_profile.get("source", "sec_edgar"),
            "as_of": now.date().isoformat(),
            "currency": existing_profile.get("currency", "USD"),
        }
        ds = _deal_size(event.payload or {})
        if ds:
            seeded["deal_size"] = ds
        payload["profile"] = seeded
        event.payload = payload

        outcome = await describe_ipo_event(
            event,
            summarizer=summarizer,
            fetch_prospectus=_fetch,
            exchange=event.exchange,
            imminent=event.id in pending_ids,
            websearch_enabled=bool(eff.ipo_websearch_provisional_enabled),
            prospectus_source="edgar",
        )
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
        log.warning("us_ipo_alert_backfill_failed", error=repr(exc))

    log.info(
        "edgar_enrich_ok",
        candidates=len(candidates),
        enriched=enriched,
        described=described,
        errors=errors,
    )
    return EdgarEnrichSummary(len(candidates), enriched, described, errors)
