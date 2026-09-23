"""Celery task wrappers around the async sync/dispatch services.

Each task runs on a fresh AsyncEngine created inside its own
``asyncio.run`` loop and disposed afterwards. This avoids reusing the
module-global pooled engine across the worker's many short-lived task
loops (which causes asyncpg "attached to a different loop" errors).
"""

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from catalyst_radar.celery_app import celery_app
from catalyst_radar.config import settings
from catalyst_radar.logging import get_logger

log = get_logger(__name__)


def _run(job: Callable[[AsyncSession], Coroutine[Any, Any, Any]]) -> Any:
    async def main() -> Any:
        engine = create_async_engine(
            settings.database_url,
            pool_pre_ping=True,
            connect_args={"statement_cache_size": 0},
        )
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        try:
            async with factory() as session:
                return await job(session)
        finally:
            # Close module-cached OpenAI clients INSIDE this task's loop.
            # Celery runs one fresh loop per task; an httpx pool that
            # outlives its loop gets aclose()d by the SDK's __del__ on a
            # later task's loop → 'Task exception was never retrieved'.
            from catalyst_radar.services.affected_companies import (
                aclose_shared_assessment_client,
            )

            await aclose_shared_assessment_client()
            await engine.dispose()

    return asyncio.run(main())


@celery_app.task(name="catalyst_radar.sync_company_reference")
def task_sync_company_reference() -> dict[str, Any]:
    from catalyst_radar.services.company_sync import sync_company_reference

    s = _run(sync_company_reference)
    log.info("task_sync_company_reference", upserted=s.total_upserted)
    return {"upserted": s.total_upserted, "errors": s.errors}


@celery_app.task(name="catalyst_radar.sync_earnings")
def task_sync_earnings() -> dict[str, Any]:
    from catalyst_radar.services.dispatch import deliver_pending_notifications
    from catalyst_radar.services.earnings_sync import sync_earnings

    async def job(session: AsyncSession) -> dict[str, Any]:
        s = await sync_earnings(session)
        d = await deliver_pending_notifications(session)
        return {"matched": s.matched, "sent": d.sent, "failed": d.failed}

    out = _run(job)
    log.info("task_sync_earnings", **out)
    return out


@celery_app.task(name="catalyst_radar.prune_source_runs")
def task_prune_source_runs() -> dict[str, Any]:
    from catalyst_radar.services.retention import prune_source_runs

    async def job(session: AsyncSession) -> dict[str, Any]:
        r = await prune_source_runs(session)
        return {
            "source_runs": r.source_runs,
            "raw_items": r.raw_items,
            "retention_days": r.retention_days,
        }

    out = _run(job)
    log.info("task_prune_source_runs", **out)
    return out


@celery_app.task(name="catalyst_radar.digests")
def task_digests() -> dict[str, Any]:
    from catalyst_radar.services.digest import run_digests
    from catalyst_radar.services.ipo_reminders import run_day_of_reminders

    async def job(session: AsyncSession) -> dict[str, Any]:
        s = await run_digests(session)
        r = await run_day_of_reminders(session)
        return {
            "daily_sent": s.daily_sent,
            "weekly_sent": s.weekly_sent,
            "skipped": s.skipped,
            "day_of_reminded": r.reminded,
            "day_of_skipped": r.skipped,
        }

    out = _run(job)
    log.info("task_digests", **out)
    return out


@celery_app.task(name="catalyst_radar.health_monitor")
def task_health_monitor() -> dict[str, Any]:
    from catalyst_radar.services.monitoring import run_health_monitor

    async def job(session: AsyncSession) -> dict[str, Any]:
        s = await run_health_monitor(session)
        return {
            "stale_alert": s.stale_alert,
            "stale_recovered": s.stale_recovered,
            "quota_alert": s.quota_alert,
            "sent": s.sent,
            "skipped": s.skipped,
        }

    out = _run(job)
    log.info("task_health_monitor", **out)
    return out


@celery_app.task(name="catalyst_radar.sync_ipos")
def task_sync_ipos() -> dict[str, Any]:
    from catalyst_radar.services.dispatch import deliver_pending_notifications
    from catalyst_radar.services.edgar_enrich import enrich_us_ipos
    from catalyst_radar.services.ipo_summarizer import IpoSummarizer
    from catalyst_radar.services.ipo_sync import sync_ipos

    async def job(session: AsyncSession) -> dict[str, Any]:
        s = await sync_ipos(session)
        # Fill in payload.profile.description for newly-created US IPOs
        # BEFORE dispatch — dispatch re-renders the alert text from the
        # event, so a description added here flows into the first alert.
        # Without this in-task chain, the first reminder-window notification
        # ships with no blurb because edgar-enrich is a separate beat task.
        summarizer = IpoSummarizer()  # owned here; closed in this loop (see _run)
        try:
            e = await enrich_us_ipos(session, summarizer=summarizer)
        finally:
            await summarizer.aclose()
        d = await deliver_pending_notifications(session)
        return {
            "matched": s.matched,
            "described": e.described,
            "sent": d.sent,
            "failed": d.failed,
        }

    out = _run(job)
    log.info("task_sync_ipos", **out)
    return out


@celery_app.task(name="catalyst_radar.sync_hk_ipos")
def task_sync_hk_ipos() -> dict[str, Any]:
    from catalyst_radar.services.dispatch import deliver_pending_notifications
    from catalyst_radar.services.hk_ipo_enrich import enrich_hk_ipos, flip_listed_hk_ipos
    from catalyst_radar.services.hk_ipo_sync import sync_hk_ipos
    from catalyst_radar.services.ipo_summarizer import IpoSummarizer

    async def job(session: AsyncSession) -> dict[str, Any]:
        # Flip listed IPOs BEFORE the sync — sync_hk_ipos only skips events
        # already at status="listed" (its docstring states this ordering
        # contract), so without a preceding flip the first run after a listing
        # creates (and delivers) a day-of reminder for an IPO that is already
        # trading. Any future direct caller of sync_hk_ipos must keep this
        # flip-first order.
        f = await flip_listed_hk_ipos(session)
        s = await sync_hk_ipos(session)
        summarizer = IpoSummarizer()  # owned here; closed in this loop (see _run)
        try:
            e = await enrich_hk_ipos(session, summarizer=summarizer)
        finally:
            await summarizer.aclose()
        d = await deliver_pending_notifications(session)
        return {
            "matched": s.matched,
            "described": e.described,
            "listed": f.flipped,
            "sent": d.sent,
            "failed": d.failed,
        }

    out = _run(job)
    log.info("task_sync_hk_ipos", **out)
    return out


@celery_app.task(name="catalyst_radar.enrich_us_ipos")
def task_enrich_us_ipos() -> dict[str, Any]:
    from catalyst_radar.services.edgar_enrich import enrich_us_ipos
    from catalyst_radar.services.ipo_summarizer import IpoSummarizer

    async def job(session: AsyncSession) -> dict[str, Any]:
        summarizer = IpoSummarizer()  # owned here; closed in this loop (see _run)
        try:
            s = await enrich_us_ipos(session, summarizer=summarizer)
        finally:
            await summarizer.aclose()
        return {
            "candidates": s.candidates,
            "enriched": s.enriched,
            "described": s.described,
            "errors": s.errors,
        }

    out = _run(job)
    log.info("task_enrich_us_ipos", **out)
    return out


@celery_app.task(name="catalyst_radar.enrich_hk_ipos")
def task_enrich_hk_ipos() -> dict[str, Any]:
    from catalyst_radar.services.hk_ipo_enrich import enrich_hk_ipos, flip_listed_hk_ipos
    from catalyst_radar.services.ipo_summarizer import IpoSummarizer

    async def job(session: AsyncSession) -> dict[str, Any]:
        summarizer = IpoSummarizer()  # owned here; closed in this loop (see _run)
        try:
            s = await enrich_hk_ipos(session, summarizer=summarizer)
        finally:
            await summarizer.aclose()
        f = await flip_listed_hk_ipos(session)
        return {
            "candidates": s.candidates,
            "enriched": s.enriched,
            "described": s.described,
            "listed": f.flipped,
            "errors": s.errors,
        }

    out = _run(job)
    log.info("task_enrich_hk_ipos", **out)
    return out


@celery_app.task(name="catalyst_radar.sync_cn_ipos")
def task_sync_cn_ipos() -> dict[str, Any]:
    from catalyst_radar.services.cn_ipo_enrich import enrich_cn_ipos
    from catalyst_radar.services.cn_ipo_sync import sync_cn_ipos
    from catalyst_radar.services.dispatch import deliver_pending_notifications
    from catalyst_radar.services.ipo_summarizer import IpoSummarizer

    async def job(session: AsyncSession) -> dict[str, Any]:
        s = await sync_cn_ipos(session)
        summarizer = IpoSummarizer()  # owned here; closed in this loop (see _run)
        try:
            e = await enrich_cn_ipos(session, summarizer=summarizer)
        finally:
            await summarizer.aclose()
        d = await deliver_pending_notifications(session)
        return {
            "matched": s.matched,
            "described": e.described,
            "sent": d.sent,
            "failed": d.failed,
        }

    out = _run(job)
    log.info("task_sync_cn_ipos", **out)
    return out


@celery_app.task(name="catalyst_radar.enrich_cn_ipos")
def task_enrich_cn_ipos() -> dict[str, Any]:
    from catalyst_radar.services.cn_ipo_enrich import enrich_cn_ipos
    from catalyst_radar.services.ipo_summarizer import IpoSummarizer

    async def job(session: AsyncSession) -> dict[str, Any]:
        summarizer = IpoSummarizer()  # owned here; closed in this loop (see _run)
        try:
            s = await enrich_cn_ipos(session, summarizer=summarizer)
        finally:
            await summarizer.aclose()
        return {
            "candidates": s.candidates,
            "enriched": s.enriched,
            "described": s.described,
            "errors": s.errors,
        }

    out = _run(job)
    log.info("task_enrich_cn_ipos", **out)
    return out


@celery_app.task(name="catalyst_radar.sync_cn_ipo_review")
def task_sync_cn_ipo_review() -> dict[str, Any]:
    from catalyst_radar.services.cn_ipo_enrich import enrich_cn_ipos
    from catalyst_radar.services.cn_ipo_review_sync import sync_cn_ipo_review
    from catalyst_radar.services.dispatch import deliver_pending_notifications
    from catalyst_radar.services.ipo_summarizer import IpoSummarizer

    async def job(session: AsyncSession) -> dict[str, Any]:
        s = await sync_cn_ipo_review(session)
        # Enrich-before-dispatch (same ordering fix as task_sync_cn_ipos,
        # #10): review-stage alerts otherwise ship in the same run that
        # creates them, before any enrich tick can fill the description.
        summarizer = IpoSummarizer()  # owned here; closed in this task loop (see _run)
        try:
            e = await enrich_cn_ipos(session, summarizer=summarizer)
        finally:
            await summarizer.aclose()
        d = await deliver_pending_notifications(session)
        return {
            "fetched": s.fetched,
            "events_created": s.events_created,
            "matched": s.matched,
            "notifications": s.notifications_created,
            "described": e.described,
            "sent": d.sent,
            "failed": d.failed,
        }

    out = _run(job)
    log.info("task_sync_cn_ipo_review", **out)
    return out


@celery_app.task(name="catalyst_radar.sync_cn_unlocks")
def task_sync_cn_unlocks() -> dict[str, Any]:
    from catalyst_radar.services.cn_unlocks_sync import sync_cn_unlocks
    from catalyst_radar.services.dispatch import deliver_pending_notifications

    async def job(session: AsyncSession) -> dict[str, Any]:
        s = await sync_cn_unlocks(session)
        d = await deliver_pending_notifications(session)
        return {
            "companies": s.companies_checked,
            "upcoming": s.upcoming_total,
            "events_created": s.events_created,
            "notifications": s.notifications_created,
            "errors": s.errors,
            "sent": d.sent,
            "failed": d.failed,
        }

    out = _run(job)
    log.info("task_sync_cn_unlocks", **out)
    return out


@celery_app.task(name="catalyst_radar.sync_catalysts")
def task_sync_catalysts() -> dict[str, Any]:
    from catalyst_radar.services.catalyst_sync import sync_catalysts
    from catalyst_radar.services.dispatch import deliver_pending_notifications
    from catalyst_radar.services.openai_classifier import OpenAIClassifier

    async def job(session: AsyncSession) -> dict[str, Any]:
        # Own the classifier here so its pooled HTTP client is closed
        # inside this task's loop (see _run for why).
        classifier = OpenAIClassifier()
        try:
            s = await sync_catalysts(session, classifier=classifier)
        finally:
            await classifier.aclose()
        d = await deliver_pending_notifications(session)
        return {
            "catalysts": s.catalysts,
            "autosent": s.autosent,
            "review": s.review,
            "deduped": s.deduped,
            "sent": d.sent,
        }

    out = _run(job)
    log.info("task_sync_catalysts", **out)
    return out


@celery_app.task(name="catalyst_radar.run_preflight")
def task_run_preflight(tracked_company_id: int) -> dict[str, Any]:
    """Run entity preflight asynchronously for a freshly tracked
    company. Best-effort: a preflight failure must never affect the
    user's tracked-company add. Mirrors the discover_sources pattern.
    """
    from catalyst_radar.repositories.company_repository import TrackedCompanyRepository
    from catalyst_radar.repositories.entity_repository import (
        CompanyEntityRepository,
        RelationshipSuggestionRepository,
    )
    from catalyst_radar.services.entity_preflight import EntityPreflight

    async def job(session: AsyncSession) -> dict[str, Any]:
        tc = await TrackedCompanyRepository(session).get(tracked_company_id)
        if tc is None:
            return {"status": "skipped", "reason": "tracked company not found"}
        preflight = EntityPreflight()
        if not preflight.configured:
            return {"status": "skipped", "reason": "openai not configured"}
        try:
            result = await preflight.preflight(
                company_name=tc.company_name,
                symbol=tc.symbol,
                exchange=tc.exchange,
                country=tc.country,
            )
        finally:
            await preflight.aclose()  # close in this loop (see _run)
        if result.status != "completed" or result.output is None:
            return {"status": "failed", "error": result.error or result.status}

        out = result.output
        if tc.entity_id:
            entity = await CompanyEntityRepository(session).get(tc.entity_id)
            if entity is not None:
                if not entity.summary and out.entity.summary:
                    entity.summary = out.entity.summary
                if not entity.country and out.entity.country:
                    entity.country = out.entity.country
                session.add(entity)
                await session.flush()

        payload_dict = {
            "entity": out.entity.model_dump(),
            "parents": [r.model_dump() for r in out.parents],
            "joint_venture_partners": [
                r.model_dump() for r in out.joint_venture_partners
            ],
            "major_shareholders": [
                r.model_dump() for r in out.major_shareholders
            ],
            "sources": result.sources,
            "notes": out.notes,
            "model": result.model,
        }
        # When preflight returns zero actionable suggestions (no parents,
        # no JV partners, no strategic shareholders) the row is just
        # "ran preflight, found nothing to add" — there's nothing for
        # the user to accept. Auto-mark it accepted-with-empty-payload
        # so it doesn't sit in the pending inbox forever. The row is
        # still kept for audit ("did we run preflight on this name?").
        sug_repo = RelationshipSuggestionRepository(session)
        sug = await sug_repo.create(
            source="preflight",
            tracked_company_id=tc.id,
            payload=payload_dict,
        )
        empty = not (out.parents or out.joint_venture_partners or out.major_shareholders)
        if empty:
            await sug_repo.mark_decided(
                sug.id,
                status="accepted",
                notes="auto:no_relationships_found_by_preflight",
            )
        await session.commit()
        return {
            "status": "completed",
            "parents": len(out.parents),
            "jvs": len(out.joint_venture_partners),
            "shareholders": len(out.major_shareholders),
            "auto_dismissed": empty,
        }

    out = _run(job)
    log.info("task_run_preflight", tracked_company_id=tracked_company_id, **out)
    return out


@celery_app.task(name="catalyst_radar.discover_sources")
def task_discover_sources(tracked_company_id: int) -> dict[str, Any]:
    from catalyst_radar.repositories.company_repository import TrackedCompanyRepository
    from catalyst_radar.services.source_discovery import discover_sources_for_company

    async def job(session: AsyncSession) -> dict[str, Any]:
        company = await TrackedCompanyRepository(session).get(tracked_company_id)
        if company is None:
            return {"status": "skipped", "reason": "company not found"}
        s = await discover_sources_for_company(session, company)
        return {
            "status": s.status,
            "provider": s.provider,
            "discovered": s.discovered,
            "created": s.created,
            "error": s.error,
        }

    out = _run(job)
    log.info("task_discover_sources", tracked_company_id=tracked_company_id, **out)
    return out


@celery_app.task(name="catalyst_radar.dispatch")
def task_dispatch() -> dict[str, Any]:
    from catalyst_radar.services.dispatch import deliver_pending_notifications

    d = _run(deliver_pending_notifications)
    return {"sent": d.sent, "failed": d.failed, "skipped": d.skipped}


@celery_app.task(name="catalyst_radar.telegram_poll")
def task_telegram_poll() -> int:
    from catalyst_radar.services.telegram_bot import poll_telegram

    return _run(poll_telegram)
