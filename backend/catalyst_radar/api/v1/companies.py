import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status

from catalyst_radar.adapters.eodhd import (
    EodhdCompanyReferenceAdapter,
    EodhdConfigError,
)
from catalyst_radar.api.deps import CurrentUser, SessionDep
from catalyst_radar.dedup import normalize_url
from catalyst_radar.logging import get_logger
from catalyst_radar.models.company import TrackedCompany
from catalyst_radar.repositories.company_repository import (
    CompanyReferenceRepository,
    TrackedCompanyRepository,
)
from catalyst_radar.repositories.company_source_repository import CompanySourceRepository
from catalyst_radar.repositories.entity_repository import (
    CompanyEntityRepository,
)
from catalyst_radar.runtime_config import effective
from catalyst_radar.schemas.company import (
    CompanyReferenceOut,
    CompanySourceCreate,
    CompanySourceOut,
    CompanySourceUpdate,
    TrackCompanyRequest,
    TrackedCompanyOut,
)

router = APIRouter(prefix="/companies", tags=["companies"])
log = get_logger(__name__)


@router.get("/search")
async def search_companies(
    current_user: CurrentUser,
    session: SessionDep,
    q: Annotated[str | None, Query()] = None,
    country: Annotated[str | None, Query()] = None,
    exchange: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> dict[str, Any]:
    repo = CompanyReferenceRepository(session)
    rows = await repo.search(q=q, country=country, exchange=exchange, limit=limit)
    results = [CompanyReferenceOut.model_validate(r) for r in rows]

    source = "reference"
    if not results and q:
        # Fallback resolver only when the local universe misses (PRD).
        try:
            adapter = EodhdCompanyReferenceAdapter()
            fetched = await adapter.search(q)
            source = "eodhd_search"
            results = [
                CompanyReferenceOut(
                    id=0,
                    symbol=n["symbol"],
                    exchange=n["exchange"],
                    country=n["country"],
                    company_name=n["company_name"],
                    sector=None,
                    industry=None,
                    currency=n["currency"],
                    isin=n["isin"],
                    source="eodhd_search",
                )
                for n in (adapter.normalize(r) for r in fetched.items)
            ][:limit]
        except EodhdConfigError:
            source = "reference"

    return {"source": source, "count": len(results), "results": results}


@router.get("/tracked", response_model=list[TrackedCompanyOut])
async def list_tracked(current_user: CurrentUser, session: SessionDep) -> list[TrackedCompany]:
    return await TrackedCompanyRepository(session).list_active()


@router.post("/tracked", response_model=TrackedCompanyOut, status_code=status.HTTP_201_CREATED)
async def track_company(
    payload: TrackCompanyRequest,
    current_user: CurrentUser,
    session: SessionDep,
) -> TrackedCompany:
    tracked_repo = TrackedCompanyRepository(session)

    if payload.company_reference_id is not None:
        ref = await CompanyReferenceRepository(session).get(payload.company_reference_id)
        if ref is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="company_reference not found",
            )
        company = TrackedCompany(
            company_reference_id=ref.id,
            symbol=ref.symbol,
            exchange=ref.exchange,
            country=ref.country,
            company_name=ref.company_name,
            sector=ref.sector,
            themes=payload.themes,
            aliases=ref.aliases or payload.aliases,
            source="reference",
        )
    else:
        if not (payload.symbol and payload.exchange and payload.company_name):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="symbol, exchange, and company_name are required for manual entry",
            )
        company = TrackedCompany(
            symbol=payload.symbol,
            exchange=payload.exchange,
            country=payload.country,
            company_name=payload.company_name,
            sector=payload.sector,
            themes=payload.themes,
            aliases=payload.aliases,
            source="manual",
        )

    existing = await tracked_repo.get_by_exchange_symbol(company.exchange, company.symbol)
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="company already tracked",
        )

    # Every tracked listing needs an entity. The minimum we can do
    # without external help is a 1:1 issuer carrying the company name;
    # if preflight is enabled and configured we replace this with a
    # richer LLM-resolved entity (and enqueue a relationship suggestion
    # for review) before committing.
    cfg = await effective(session)
    ent_repo = CompanyEntityRepository(session)
    entity = await ent_repo.upsert_by_name(
        canonical_name=company.company_name,
        country=company.country,
        source="track_default_1to1",
    )
    company.entity_id = entity.id

    tracked = await tracked_repo.add(company)

    preflight_enabled = bool(getattr(cfg, "relationship_preflight_enabled", True))
    if preflight_enabled and tracked.id is not None:
        # Enqueue rather than await: the preflight LLM call uses
        # web_search and routinely takes 30-90 s, which would block the
        # HTTP response and trip nginx / load-balancer idle timeouts.
        # Mirror the discover_sources fire-and-forget pattern.
        _enqueue_preflight(tracked.id)

    if cfg.catalyst_discovery_enabled and tracked.id is not None:
        _enqueue_discovery(tracked.id)
    return tracked


def _enqueue_preflight(tracked_company_id: int) -> None:
    """Best-effort queue of entity preflight. A broker hiccup must
    never fail the track request."""
    try:
        from catalyst_radar.tasks import task_run_preflight

        task_run_preflight.delay(tracked_company_id)
    except Exception as exc:  # noqa: BLE001 - never block tracking on the queue
        log.warning(
            "preflight_enqueue_failed",
            tracked_company_id=tracked_company_id,
            error=repr(exc),
        )


def _enqueue_discovery(tracked_company_id: int) -> None:
    """Best-effort queue of source discovery. A broker hiccup must never fail
    the track request, so failures here are logged and swallowed."""
    try:
        from catalyst_radar.tasks import task_discover_sources

        task_discover_sources.delay(tracked_company_id)
    except Exception as exc:  # noqa: BLE001 - never block tracking on the queue
        log.warning(
            "discovery_enqueue_failed", tracked_company_id=tracked_company_id, error=repr(exc)
        )


@router.delete("/tracked/{tracked_id}", status_code=status.HTTP_204_NO_CONTENT)
async def untrack_company(
    tracked_id: int,
    current_user: CurrentUser,
    session: SessionDep,
    purge: Annotated[bool, Query()] = False,
) -> None:
    repo = TrackedCompanyRepository(session)
    # Capture the entity_id before deactivate so we can sweep orphan
    # parent_only listings (auto-tracked parents that exist only to
    # carry news for a child the user just removed).
    tc = await repo.get(tracked_id)
    if tc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="tracked company not found"
        )
    captured_entity_id = tc.entity_id
    captured_id = tc.id
    ok = await repo.delete(tracked_id) if purge else await repo.deactivate(tracked_id)
    if not ok:  # race: another request removed it between the get and the write
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="tracked company not found"
        )
    if captured_entity_id is not None:
        await _sweep_orphan_parent_only_listings(
            session,
            removed_tc_id=captured_id,
            removed_entity_id=captured_entity_id,
        )


async def _sweep_orphan_parent_only_listings(
    session: Any, *, removed_tc_id: int, removed_entity_id: int
) -> list[int]:
    """When the user removes a tracked company, walk its relationship
    graph and deactivate any neighbour entity's `is_parent_only`
    listings that no longer have a non-parent-only consumer left. The
    purpose of a parent_only listing is to feed news for *some other*
    tracked company; once nothing tracks anything connected to it, it
    just burns LLM budget on irrelevant news.

    Returns the list of deactivated tracked_company ids."""
    from sqlalchemy import select

    from catalyst_radar.models.entity import EntityRelationship

    edges = (
        await session.execute(
            select(EntityRelationship).where(
                (EntityRelationship.from_entity_id == removed_entity_id)
                | (EntityRelationship.to_entity_id == removed_entity_id)
            )
        )
    ).scalars().all()
    neighbour_eids: set[int] = set()
    for e in edges:
        for eid in (e.from_entity_id, e.to_entity_id):
            if eid != removed_entity_id:
                neighbour_eids.add(eid)
    deactivated: list[int] = []
    for neighbour_id in neighbour_eids:
        listings = (
            await session.execute(
                select(TrackedCompany).where(
                    TrackedCompany.entity_id == neighbour_id,
                    TrackedCompany.is_active.is_(True),
                )
            )
        ).scalars().all()
        if not listings:
            continue
        # If ANY listing on this neighbour is non-parent-only, the user
        # legitimately tracks the entity; leave it alone.
        if any(not tc.is_parent_only for tc in listings):
            continue
        # All listings on neighbour are parent_only. Walk back out from
        # neighbour to find any active non-parent-only tracked company
        # (other than the one we just removed) that justifies keeping it.
        peer_edges = (
            await session.execute(
                select(EntityRelationship).where(
                    (EntityRelationship.from_entity_id == neighbour_id)
                    | (EntityRelationship.to_entity_id == neighbour_id)
                )
            )
        ).scalars().all()
        peer_eids: set[int] = set()
        for pe in peer_edges:
            for eid in (pe.from_entity_id, pe.to_entity_id):
                if eid != neighbour_id:
                    peer_eids.add(eid)
        if not peer_eids:
            consumers: list[TrackedCompany] = []
        else:
            consumers = list(
                (
                    await session.execute(
                        select(TrackedCompany).where(
                            TrackedCompany.entity_id.in_(peer_eids),
                            TrackedCompany.is_active.is_(True),
                            TrackedCompany.is_parent_only.is_(False),
                            TrackedCompany.id != removed_tc_id,
                        )
                    )
                ).scalars().all()
            )
        if consumers:
            continue
        for tc in listings:
            tc.is_active = False
            session.add(tc)
            deactivated.append(tc.id)
    if deactivated:
        await session.commit()
        log.info("parent_only_swept", removed=removed_tc_id, deactivated=deactivated)
    return deactivated


# ── Company sources  ────────────────────────────────────────


async def _require_tracked(session: SessionDep, tracked_id: int) -> TrackedCompany:
    company = await TrackedCompanyRepository(session).get(tracked_id)
    if company is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="tracked company not found"
        )
    return company


@router.get("/tracked/{tracked_id}/sources", response_model=list[CompanySourceOut])
async def list_company_sources(
    tracked_id: int,
    current_user: CurrentUser,
    session: SessionDep,
    include_inactive: Annotated[bool, Query()] = False,
) -> list[Any]:
    await _require_tracked(session, tracked_id)
    return await CompanySourceRepository(session).list_for_company(
        tracked_id, include_inactive=include_inactive
    )


@router.post(
    "/tracked/{tracked_id}/sources",
    response_model=CompanySourceOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_company_source(
    tracked_id: int,
    payload: CompanySourceCreate,
    current_user: CurrentUser,
    session: SessionDep,
) -> Any:
    await _require_tracked(session, tracked_id)
    repo = CompanySourceRepository(session)
    url = normalize_url(str(payload.url))
    if await repo.get_by_company_url(tracked_id, url) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="source url already exists"
        )
    return await repo.create(
        tracked_company_id=tracked_id,
        kind=payload.kind,
        url=url,
        label=payload.label,
        fetch_strategy=payload.fetch_strategy,
        source="manual",
    )


@router.patch("/sources/{source_id}", response_model=CompanySourceOut)
async def update_company_source(
    source_id: int,
    payload: CompanySourceUpdate,
    current_user: CurrentUser,
    session: SessionDep,
) -> Any:
    repo = CompanySourceRepository(session)
    record = await repo.get(source_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="source not found")
    fields = payload.model_dump(exclude_unset=True)
    if fields.get("url") is not None:
        new_url = normalize_url(str(fields["url"]))
        fields["url"] = new_url
        # Guard the (tracked_company_id, url) unique constraint with a clean 409
        # instead of letting the DB raise IntegrityError (500 + poisoned session).
        clash = await repo.get_by_company_url(record.tracked_company_id, new_url)
        if clash is not None and clash.id != source_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="source url already exists"
            )
    updated = await repo.update(source_id, **fields)
    return updated


@router.delete("/sources/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_company_source(
    source_id: int,
    current_user: CurrentUser,
    session: SessionDep,
) -> None:
    if not await CompanySourceRepository(session).delete(source_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="source not found")


@router.post("/tracked/{tracked_id}/discover-sources")
async def discover_company_sources(
    tracked_id: int,
    current_user: CurrentUser,
    session: SessionDep,
) -> dict[str, Any]:
    """Run source discovery for one company now and return the summary.

    Synchronous so the UI can show the result immediately, but bounded so a slow
    provider can't pin an HTTP worker + DB session indefinitely.
    """
    from catalyst_radar.services.source_discovery import discover_sources_for_company

    company = await _require_tracked(session, tracked_id)
    try:
        summary = await asyncio.wait_for(
            discover_sources_for_company(session, company), timeout=120
        )
    except TimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail="discovery timed out"
        ) from exc
    if summary.status != "completed":
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=summary.error or "discovery failed",
        )
    return {
        "status": summary.status,
        "provider": summary.provider,
        "discovered": summary.discovered,
        "created": summary.created,
    }
