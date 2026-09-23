"""Enrich HK IPO events with a prospectus-based one-liner (Phase 9.6b)
and flip them to ``listed`` once trading starts (Phase 9.5b).

Mirror of edgar_enrich for Hong Kong: resolve the HKEXnews prospectus
for each recent HK IPO lacking a profile, extract its SUMMARY slice,
LLM-summarize, and write ``payload['profile']``. Idempotent (events
marked checked). HKEXnews downloads are slow, so the per-run budget is
small.

``flip_listed_hk_ipos`` closes the lifecycle: HK IPO events whose
stock code appears in the HKEXnews New Listing Report flip to
``status="listed"`` (idempotent — listed rows are excluded from the
candidate query), which stops further upcoming-IPO reminders in
``hk_ipo_sync``. It rides the same Celery tasks as this enricher — no
beat entry of its own.
"""

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.adapters.hk_codes import pad_code
from catalyst_radar.adapters.hkex_newly_listed import HkexNewlyListedAdapter
from catalyst_radar.adapters.hkex_prospectus import HkexProspectusAdapter
from catalyst_radar.logging import get_logger
from catalyst_radar.models.base import utcnow
from catalyst_radar.models.event import Event
from catalyst_radar.repositories.event_repository import EventRepository
from catalyst_radar.repositories.notification_repository import NotificationRepository
from catalyst_radar.repositories.source_repository import (
    RawItemRepository,
    SourceRunRepository,
)
from catalyst_radar.services.ipo_summarizer import IpoSummarizer
from catalyst_radar.services.ipo_sync import select_enrich_candidates

log = get_logger(__name__)


@dataclass(slots=True)
class HkIpoEnrichSummary:
    candidates: int
    enriched: int
    described: int
    errors: int


async def enrich_hk_ipos(
    session: AsyncSession,
    adapter: HkexProspectusAdapter | None = None,
    summarizer: IpoSummarizer | None = None,
) -> HkIpoEnrichSummary:
    from catalyst_radar.config import settings
    from catalyst_radar.runtime_config import effective

    eff = await effective(session)
    if not bool(eff.hkex_prospectus_enrich_enabled):
        log.info("hk_ipo_enrich_disabled")
        return HkIpoEnrichSummary(0, 0, 0, 0)

    budget = int(settings.hkex_prospectus_max_items_per_run)
    adapter = adapter or HkexProspectusAdapter()
    summarizer = summarizer or IpoSummarizer()
    runs = SourceRunRepository(session)
    run = await runs.start("hkexnews.prospectus_enrich")

    now = utcnow()
    stmt = (
        select(Event)
        .where(
            Event.event_type == "ipo",
            Event.country == "HK",
            Event.symbol.is_not(None),
            Event.event_date.is_not(None),
            Event.event_date >= now - timedelta(days=14),
            Event.event_date <= now + timedelta(days=120),
        )
        .order_by(Event.event_date)
        .limit(100)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    # Pending-notification events first — an AAStocks recovery mints
    # several IPOs at once and a plain budget clip ships some of their
    # alerts blurbless (see ipo_sync.select_enrich_candidates).
    candidates = await select_enrich_candidates(session, rows, budget=budget)

    # Precise 'about to be alerted' set: events whose notification is pending
    # dispatch get the fast web_search provisional FIRST (so the first alert
    # carries a blurb), instead of waiting on the slow prospectus PDF.
    pending_ids = await NotificationRepository(session).pending_event_ids()

    from catalyst_radar.services.ipo_describe import describe_ipo_event

    async def _fetch(e: Event) -> dict | None:
        return await adapter.fetch_prospectus(e.symbol or "")

    enriched = described = errors = 0
    for event in candidates:
        # Seed HK-specific profile fields the shared helper does not set
        # (source/as_of/currency); the helper copies event.payload["profile"]
        # before writing description/meta, so these are preserved.
        existing_profile = (event.payload or {}).get("profile") or {}
        payload = dict(event.payload or {})
        payload["profile"] = {
            **existing_profile,
            "source": existing_profile.get("source", "hkexnews"),
            "as_of": now.date().isoformat(),
            "currency": existing_profile.get("currency", "HKD"),
        }
        event.payload = payload

        outcome = await describe_ipo_event(
            event,
            summarizer=summarizer,
            fetch_prospectus=_fetch,
            exchange="HKSE",
            imminent=event.id in pending_ids,
            websearch_enabled=bool(eff.ipo_websearch_provisional_enabled),
            prospectus_source="prospectus",
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
        log.warning("hk_ipo_alert_backfill_failed", error=repr(exc))

    log.info(
        "hk_ipo_enrich_ok",
        candidates=len(candidates),
        enriched=enriched,
        described=described,
        errors=errors,
    )
    return HkIpoEnrichSummary(len(candidates), enriched, described, errors)


@dataclass(slots=True)
class HkIpoListedFlipSummary:
    listed_codes: int
    flipped: int


async def flip_listed_hk_ipos(
    session: AsyncSession,
    adapter: HkexNewlyListedAdapter | None = None,
) -> HkIpoListedFlipSummary:
    """Flip open HK IPO events to ``status="listed"`` once their stock
    code appears in the HKEXnews New Listing Report (Phase 9.5b).

    Idempotent: already-listed rows are excluded from the candidate
    query, so a second run is a no-op. User-ignored rows are left alone
    — flipping them would resurrect a deliberate decision.
    """
    from catalyst_radar.runtime_config import effective

    eff = await effective(session)
    if not bool(eff.hkex_listed_flip_enabled):
        log.info("hk_ipo_listed_flip_disabled")
        return HkIpoListedFlipSummary(0, 0)

    adapter = adapter or HkexNewlyListedAdapter()
    runs = SourceRunRepository(session)
    run = await runs.start("hkexnews.newly_listed")

    try:
        result = await adapter.fetch()
    except Exception as exc:  # noqa: BLE001
        await runs.finish(run, status="failed", last_error=repr(exc))
        await session.commit()
        log.warning("hk_ipo_listed_flip_fetch_failed", error=repr(exc))
        return HkIpoListedFlipSummary(0, 0)

    await RawItemRepository(session).store(
        source_name=result.source_name,
        schema_name=result.schema_name,
        raw_payload=result.payload,
        source_url=result.source_url,
        http_status=result.http_status,
        source_run_id=run.id,
    )
    if result.http_status != 200 or not result.items:
        await runs.finish(
            run,
            status="empty" if result.http_status == 200 else "failed",
            last_error=None if result.http_status == 200 else f"http {result.http_status}",
        )
        await session.commit()
        return HkIpoListedFlipSummary(len(result.items), 0)

    listed = {row["code"]: row for row in result.items if row.get("code")}

    now = utcnow()
    stmt = (
        select(Event)
        .where(
            Event.event_type == "ipo",
            Event.country == "HK",
            Event.symbol.is_not(None),
            Event.status.not_in(["listed", "ignored"]),
            or_(
                Event.event_date.is_(None),
                Event.event_date >= now - timedelta(days=90),
            ),
        )
        .order_by(Event.event_date)
        .limit(500)
    )
    candidates = list((await session.execute(stmt)).scalars().all())

    events_repo = EventRepository(session)
    flipped = 0
    for event in candidates:
        row = listed.get(pad_code(event.symbol) or "")
        if row is None:
            continue
        try:
            await events_repo.update_status_and_payload(
                event,
                status="listed",
                payload_merge={
                    "listing_confirmed": {
                        "listing_date": row.get("listing_date"),
                        "source": "hkexnews.newly_listed",
                        "as_of": now.date().isoformat(),
                    }
                },
            )
            flipped += 1
        except Exception as exc:  # noqa: BLE001 - one bad row never aborts
            log.warning("hk_ipo_listed_flip_row_failed", code=event.symbol, error=repr(exc))
            continue

    await runs.finish(run, status="success", item_count=flipped)
    await session.commit()
    log.info("hk_ipo_listed_flip_ok", listed_codes=len(listed), flipped=flipped)
    return HkIpoListedFlipSummary(len(listed), flipped)
