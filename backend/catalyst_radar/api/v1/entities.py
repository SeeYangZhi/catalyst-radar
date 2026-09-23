"""API for the entity layer: entities, relationship graph, preflight,
and the suggestion review queue. Powers the settings page where users
inspect / edit / accept-reject the relationships that drive catalyst
spillover."""

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status

from catalyst_radar.api.deps import CurrentUser, SessionDep
from catalyst_radar.logging import get_logger
from catalyst_radar.models.company import TrackedCompany
from catalyst_radar.models.entity import CompanyEntity, EntityRelationship
from catalyst_radar.repositories.company_repository import (
    TrackedCompanyRepository,
)
from catalyst_radar.repositories.entity_repository import (
    CompanyEntityRepository,
    EntityRelationshipRepository,
    RelationshipSuggestionRepository,
)
from catalyst_radar.runtime_config import effective
from catalyst_radar.schemas.entity import (
    CompanyEntityCreate,
    CompanyEntityOut,
    CompanyEntityUpdate,
    EntityRelationshipCreate,
    EntityRelationshipOut,
    PreflightEntity,
    PreflightRequest,
    PreflightResponse,
    RelationshipSuggestionOut,
    SuggestionDecision,
)
from catalyst_radar.services.entity_preflight import EntityPreflight

router = APIRouter(prefix="/entities", tags=["entities"])
log = get_logger(__name__)


# ── Preflight ────────────────────────────────────────────────────────


@router.post("/preflight", response_model=PreflightResponse)
async def run_preflight(
    payload: PreflightRequest,
    current_user: CurrentUser,
    session: SessionDep,
) -> PreflightResponse:
    """Resolve a company to its entity + suggest related entities. The
    suggestion (if generated) lands in the review queue keyed to the
    tracked_company_id (when provided), so the user can accept/reject
    each relationship later. Returns the LLM output inline so the UI
    can render suggestions immediately."""
    cfg = await effective(session)
    if not bool(getattr(cfg, "relationship_preflight_enabled", True)):
        return PreflightResponse(status="disabled", error="preflight disabled")

    tracked: TrackedCompany | None = None
    if payload.tracked_company_id is not None:
        tracked = await TrackedCompanyRepository(session).get(payload.tracked_company_id)
        if tracked is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="tracked company not found"
            )
        symbol = tracked.symbol
        exchange = tracked.exchange
        company_name = tracked.company_name
        country = tracked.country
    else:
        if not (payload.symbol and payload.exchange and payload.company_name):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="symbol, exchange, and company_name required when no tracked_company_id",
            )
        symbol = payload.symbol
        exchange = payload.exchange
        company_name = payload.company_name
        country = payload.country

    preflight = EntityPreflight()
    if not preflight.configured:
        return PreflightResponse(status="failed", error="OPENAI_API_KEY not configured")

    try:
        result = await preflight.preflight(
            company_name=company_name,
            symbol=symbol,
            exchange=exchange,
            country=country,
        )
    finally:
        await preflight.aclose()

    if result.status != "completed" or result.output is None:
        return PreflightResponse(
            status="failed", error=result.error, model=result.model
        )

    out = result.output
    suggestion_payload = {
        "entity": out.entity.model_dump(),
        "parents": [r.model_dump() for r in out.parents],
        "joint_venture_partners": [r.model_dump() for r in out.joint_venture_partners],
        "major_shareholders": [r.model_dump() for r in out.major_shareholders],
        "sources": result.sources,
        "notes": out.notes,
        "model": result.model,
    }
    suggestion = await RelationshipSuggestionRepository(session).create(
        source="preflight",
        tracked_company_id=tracked.id if tracked is not None else None,
        payload=suggestion_payload,
    )
    await session.commit()

    return PreflightResponse(
        status="completed",
        entity=PreflightEntity(**out.entity.model_dump()),
        parents=[PreflightEntity(**r.model_dump()) for r in out.parents],
        joint_venture_partners=[
            PreflightEntity(**r.model_dump()) for r in out.joint_venture_partners
        ],
        major_shareholders=[
            PreflightEntity(**r.model_dump()) for r in out.major_shareholders
        ],
        sources=result.sources,
        notes=out.notes,
        model=result.model,
        suggestion_id=suggestion.id,
    )


# ── Entities CRUD ────────────────────────────────────────────────────


@router.get("", response_model=list[CompanyEntityOut])
async def list_entities(
    current_user: CurrentUser, session: SessionDep
) -> list[CompanyEntity]:
    return await CompanyEntityRepository(session).list()


@router.post("", response_model=CompanyEntityOut, status_code=status.HTTP_201_CREATED)
async def create_entity(
    payload: CompanyEntityCreate,
    current_user: CurrentUser,
    session: SessionDep,
) -> CompanyEntity:
    entity = await CompanyEntityRepository(session).create(
        canonical_name=payload.canonical_name,
        country=payload.country,
        summary=payload.summary,
    )
    await session.commit()
    return entity


@router.patch("/{entity_id}", response_model=CompanyEntityOut)
async def update_entity(
    entity_id: int,
    payload: CompanyEntityUpdate,
    current_user: CurrentUser,
    session: SessionDep,
) -> CompanyEntity:
    entity = await CompanyEntityRepository(session).update(
        entity_id,
        canonical_name=payload.canonical_name,
        country=payload.country,
        summary=payload.summary,
    )
    if entity is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="entity not found")
    await session.commit()
    return entity


# ── Relationships ────────────────────────────────────────────────────


@router.get("/relationships", response_model=list[EntityRelationshipOut])
async def list_relationships(
    current_user: CurrentUser,
    session: SessionDep,
    entity_id: Annotated[int | None, Query()] = None,
) -> list[EntityRelationship]:
    repo = EntityRelationshipRepository(session)
    return (
        await repo.list_for_entity(entity_id) if entity_id is not None else await repo.list()
    )


@router.post(
    "/relationships",
    response_model=EntityRelationshipOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_relationship(
    payload: EntityRelationshipCreate,
    current_user: CurrentUser,
    session: SessionDep,
) -> EntityRelationship:
    if payload.from_entity_id == payload.to_entity_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="from_entity_id and to_entity_id must differ",
        )
    ent_repo = CompanyEntityRepository(session)
    for eid in (payload.from_entity_id, payload.to_entity_id):
        if await ent_repo.get(eid) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"entity {eid} not found"
            )
    rel = await EntityRelationshipRepository(session).create(
        from_entity_id=payload.from_entity_id,
        to_entity_id=payload.to_entity_id,
        kind=payload.kind,
        notes=payload.notes,
    )
    await session.commit()
    return rel


@router.delete("/relationships/{rel_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_relationship(
    rel_id: int, current_user: CurrentUser, session: SessionDep
) -> None:
    if not await EntityRelationshipRepository(session).delete(rel_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="relationship not found"
        )
    await session.commit()


# ── Suggestions ──────────────────────────────────────────────────────


@router.get("/suggestions", response_model=list[RelationshipSuggestionOut])
async def list_suggestions(
    current_user: CurrentUser,
    session: SessionDep,
    status_filter: Annotated[str | None, Query(alias="status")] = "pending",
) -> list[Any]:
    return await RelationshipSuggestionRepository(session).list(status=status_filter)


@router.post("/suggestions/{suggestion_id}/decision")
async def decide_suggestion(
    suggestion_id: int,
    payload: SuggestionDecision,
    current_user: CurrentUser,
    session: SessionDep,
) -> dict[str, Any]:
    """Accept or reject a pending suggestion.

    On accept: for each ``accepted_keys`` entry (formatted as
    ``"parents:0"`` / ``"joint_venture_partners:1"`` /
    ``"major_shareholders:2"``), upsert the referenced entity, link via
    ``entity_relationships``, and — when ``auto_track`` is set and the
    referenced entity has a ticker — auto-add the listing as
    ``is_parent_only=True`` if not already tracked.
    """
    sug_repo = RelationshipSuggestionRepository(session)
    sug = await sug_repo.get(suggestion_id)
    if sug is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="suggestion not found"
        )
    if sug.status != "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"suggestion already {sug.status}",
        )

    applied: list[dict[str, Any]] = []
    auto_tracked: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    if payload.decision == "accept":
        # Self entity for the originating tracked company (if any) →
        # used as the from/to anchor for the relationship rows.
        self_entity = await _resolve_self_entity(session, sug)
        if self_entity is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "cannot apply suggestion: no tracked_company_id linked, "
                    "or its entity is missing"
                ),
            )
        # Per-entry SAVEPOINT so a single bad entry (unique-constraint
        # violation when auto-tracking a ticker the user already
        # tracks, malformed ref, etc.) doesn't poison the whole batch
        # or leave the session in an unusable state. Successful entries
        # commit at the end with the suggestion's accepted status; any
        # failures are reported back to the caller.
        for key in payload.accepted_keys:
            try:
                async with session.begin_nested():
                    applied_row = await _apply_suggestion_entry(
                        session,
                        payload=sug.payload,
                        key=key,
                        self_entity=self_entity,
                        auto_track=payload.auto_track,
                    )
            except Exception as exc:  # noqa: BLE001 - per-entry isolation
                log.warning(
                    "suggestion_entry_failed",
                    suggestion_id=suggestion_id,
                    key=key,
                    error=repr(exc),
                )
                failed.append({"key": key, "error": str(exc)})
                continue
            if applied_row is None:
                failed.append({"key": key, "error": "malformed_key_or_index"})
                continue
            applied.append(applied_row)
            if applied_row.get("auto_tracked"):
                auto_tracked.append(applied_row)

    await sug_repo.mark_decided(
        suggestion_id,
        status="accepted" if payload.decision == "accept" else "rejected",
        decided_by_user_id=getattr(current_user, "id", None),
        notes=payload.notes,
    )
    await session.commit()
    return {
        "status": "accepted" if payload.decision == "accept" else "rejected",
        "applied": applied,
        "auto_tracked": auto_tracked,
        "failed": failed,
    }


# ── Internal helpers ─────────────────────────────────────────────────


async def _resolve_self_entity(session, sug) -> CompanyEntity | None:
    """Return the CompanyEntity tied to the suggestion's tracked
    company. Upserts one from the payload's ``entity`` block if the
    tracked company has no entity_id yet.

    Locks the tracked_companies row with SELECT FOR UPDATE on Postgres
    to serialize concurrent accept requests targeting the same tracked
    company (otherwise two concurrent decisions race to overwrite
    tc.entity_id, and the later commit wins silently — losing the
    earlier decision's relationship anchor). SQLite ignores FOR UPDATE
    and the test path runs single-threaded anyway.
    """
    if sug.tracked_company_id is None:
        return None
    from sqlalchemy import select as _select

    stmt = _select(TrackedCompany).where(TrackedCompany.id == sug.tracked_company_id)
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update()
    tc = (await session.execute(stmt)).scalar_one_or_none()
    if tc is None:
        return None
    ent_repo = CompanyEntityRepository(session)
    if tc.entity_id:
        return await ent_repo.get(tc.entity_id)
    ent_block = (sug.payload or {}).get("entity") or {}
    name = (ent_block.get("canonical_name") or tc.company_name).strip()
    entity = await ent_repo.upsert_by_name(
        canonical_name=name,
        country=ent_block.get("country") or tc.country,
        summary=ent_block.get("summary"),
        source="preflight",
    )
    tc.entity_id = entity.id
    session.add(tc)
    await session.flush()
    return entity


async def _apply_suggestion_entry(
    session,
    *,
    payload: dict[str, Any],
    key: str,
    self_entity: CompanyEntity,
    auto_track: bool,
) -> dict[str, Any] | None:
    """Apply one suggestion entry like ``parents:0``. Returns a summary
    dict for the response (or None if the key was malformed)."""
    try:
        bucket, idx_str = key.split(":", 1)
        idx = int(idx_str)
    except ValueError:
        return None
    bucket_to_kind = {
        "parents": "parent_of",
        "joint_venture_partners": "joint_venture",
        "major_shareholders": "major_shareholder",
    }
    kind = bucket_to_kind.get(bucket)
    if kind is None:
        return None
    items = payload.get(bucket) or []
    if not 0 <= idx < len(items):
        return None
    ref = items[idx]
    # Refuse to upsert garbage entities. An empty canonical_name was
    # previously fallback'd to f"unknown:{kind}:{idx}", which (a) all
    # collapsed onto the same entity when upserted by-name across two
    # suggestions, and (b) leaked a clearly-fake name into the BFS
    # graph. Better to fail loudly so the user (or the preflight model)
    # produces a usable identifier.
    canonical = (ref.get("canonical_name") or "").strip()
    if not canonical:
        raise ValueError(f"suggestion entry {kind}:{idx} has no canonical_name")
    ent_repo = CompanyEntityRepository(session)
    rel_repo = EntityRelationshipRepository(session)

    other = await ent_repo.upsert_by_name(
        canonical_name=canonical,
        country=ref.get("country") or None,
        summary=ref.get("summary"),
        source="preflight",
    )

    # Direction is determined by the schema convention that ``from``
    # holds/owns/parents ``to``. The preflight buckets enumerate
    # entities that hold THIS RELATIONSHIP TO the self entity, so:
    #   * "parents" → other is parent, self is child → (other → self)
    #   * "major_shareholders" → other holds stake in self → (other → self)
    #   * "joint_venture_partners" → symmetric; BFS traverses both ways,
    #     so the row direction is informational only → (self → other)
    if kind in ("parent_of", "major_shareholder"):
        from_id, to_id = other.id, self_entity.id
    else:
        from_id, to_id = self_entity.id, other.id

    rel = await rel_repo.create(
        from_entity_id=from_id,
        to_entity_id=to_id,
        kind=kind,
        source="preflight",
        source_payload={"ref": ref},
    )

    auto_tracked = False
    new_tracked_id: int | None = None
    # The preflight prompt asks the LLM to prefix tickers with "$" in
    # free-text fields, and the model sometimes leaks that prefix into
    # the structured EntityRef.ticker too. Strip it so the lookup and
    # the new TrackedCompany row use the bare symbol, matching how the
    # rest of the system stores tickers.
    raw_ticker = (ref.get("ticker") or "").strip()
    raw_exchange = (ref.get("exchange") or "").strip()
    bare_ticker = raw_ticker.lstrip("$").strip()
    if auto_track and bare_ticker and raw_exchange:
        tc_repo = TrackedCompanyRepository(session)
        existing = await tc_repo.get_by_exchange_symbol(raw_exchange, bare_ticker)
        if existing is None:
            tc = TrackedCompany(
                entity_id=other.id,
                symbol=bare_ticker,
                exchange=raw_exchange,
                country=ref.get("country") or None,
                company_name=ref.get("canonical_name"),
                source="preflight_auto",
                is_parent_only=True,
                is_active=True,
            )
            session.add(tc)
            await session.flush()
            auto_tracked = True
            new_tracked_id = tc.id
        elif existing.entity_id != other.id:
            # Hitch the existing tracked listing to the entity we just
            # resolved (covers the case where it was 1:1-backfilled with
            # its own entity).
            existing.entity_id = other.id
            session.add(existing)
            await session.flush()

    return {
        "kind": kind,
        "relationship_id": rel.id,
        "entity_id": other.id,
        "entity_name": other.canonical_name,
        "auto_tracked": auto_tracked,
        "tracked_company_id": new_tracked_id,
    }
