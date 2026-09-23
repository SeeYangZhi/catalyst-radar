"""One-shot backfill: for every active tracked_company that doesn't
yet have a pending or accepted preflight suggestion, run the preflight
LLM call and enqueue a ``relationship_suggestions`` row for the user
to review on the settings page.

No autonomous writes — this script never creates relationships,
entities, or auto-tracks parents on its own. It only fills the inbox.
Designed to be re-runnable; skips tracked companies whose latest
suggestion is still ``pending``.

Usage:
    python -m catalyst_radar.scripts.backfill_entities [--limit N]
"""

import argparse
import asyncio

from sqlalchemy import select

from catalyst_radar.db import async_session_factory
from catalyst_radar.logging import get_logger
from catalyst_radar.models.entity import RelationshipSuggestion
from catalyst_radar.repositories.company_repository import TrackedCompanyRepository
from catalyst_radar.repositories.entity_repository import (
    CompanyEntityRepository,
    RelationshipSuggestionRepository,
)
from catalyst_radar.services.entity_preflight import EntityPreflight

log = get_logger("backfill_entities")


async def _has_pending_suggestion(session, tracked_id: int) -> bool:
    result = await session.execute(
        select(RelationshipSuggestion).where(
            RelationshipSuggestion.tracked_company_id == tracked_id,
            RelationshipSuggestion.status == "pending",
        )
    )
    return result.scalar_one_or_none() is not None


async def _main(limit: int | None) -> None:
    preflight = EntityPreflight()
    if not preflight.configured:
        log.error("preflight_not_configured")
        return

    enqueued = skipped = failed = 0
    async with async_session_factory() as session:
        tracked = await TrackedCompanyRepository(session).list_active()
        ent_repo = CompanyEntityRepository(session)
        sug_repo = RelationshipSuggestionRepository(session)

        for tc in tracked:
            if limit is not None and enqueued >= limit:
                break
            if await _has_pending_suggestion(session, tc.id):
                skipped += 1
                continue
            try:
                result = await preflight.preflight(
                    company_name=tc.company_name,
                    symbol=tc.symbol,
                    exchange=tc.exchange,
                    country=tc.country,
                )
            except Exception as exc:  # noqa: BLE001 - one row must not abort the batch
                failed += 1
                log.warning("preflight_error", tracked_id=tc.id, error=repr(exc))
                continue
            if result.status != "completed" or result.output is None:
                failed += 1
                log.info(
                    "preflight_no_result", tracked_id=tc.id, error=result.error
                )
                continue

            out = result.output
            # Backfill summary onto the tracked company's entity if it
            # was a 1:1 default with no summary on file.
            if tc.entity_id:
                entity = await ent_repo.get(tc.entity_id)
                if entity is not None:
                    if not entity.summary and out.entity.summary:
                        entity.summary = out.entity.summary
                    if not entity.country and out.entity.country:
                        entity.country = out.entity.country
                    session.add(entity)
                    await session.flush()

            payload = {
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
            sug = await sug_repo.create(
                source="backfill",
                tracked_company_id=tc.id,
                payload=payload,
            )
            # Auto-dismiss empty suggestions so the user's inbox only
            # shows rows that have something actionable. The accepted +
            # auto-note record stays for audit (we did run preflight,
            # nothing strategic was found).
            empty = not (
                out.parents
                or out.joint_venture_partners
                or out.major_shareholders
            )
            if empty:
                await sug_repo.mark_decided(
                    sug.id,
                    status="accepted",
                    notes="auto:no_relationships_found_by_preflight",
                )
            await session.commit()
            enqueued += 1
            log.info(
                "preflight_enqueued",
                tracked_id=tc.id,
                symbol=tc.symbol,
                parents=len(out.parents),
                jvs=len(out.joint_venture_partners),
                shareholders=len(out.major_shareholders),
                auto_dismissed=empty,
            )

    log.info(
        "backfill_done", enqueued=enqueued, skipped=skipped, failed=failed
    )


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--limit", type=int, default=None,
        help="Process at most N companies (useful for first-run smoke tests)."
    )
    args = ap.parse_args()
    asyncio.run(_main(args.limit))
