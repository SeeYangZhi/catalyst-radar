"""Catalyst spillover: figure out which tracked listings a single
classified news event materially impacts, and write the
``event_affected_companies`` rows that drive the unified alert card.

Two steps, both inside one transaction off ``catalyst_sync``:

1. **BFS** the ``entity_relationships`` graph from the event's primary
   entity. Up to ``relationship_max_hops`` (default 2). ``parent_of``
   and ``major_shareholder`` are downward-only (from → to); JV edges
   traverse in both directions. Visited-set prevents cycles. The
   result is a candidate set: every entity reached, with its
   relationship role + hop distance + path.

2. **LLM assessment** (single batched call to ``gpt-5.4-mini``): for
   each candidate, decide whether *this specific news* materially
   impacts it, what importance to assign, and a one-line reason that
   must cite the news content. The call replaces the static-haircut
   approach — earnings vs. ESG vs. M&A move subsidiaries differently
   and the model is the only thing that can tell them apart.

The primary tracked listing(s) (the entity's own listings) are also
emitted as ``role=primary`` rows so the formatter can render the full
"Affects your watchlist" section from a single query. Primary rows
get the classifier's own importance — they're not haircut, they're
not LLM-reassessed (the upstream classifier already decided).

When no candidates pass the assessment AND every primary listing is
``is_parent_only=True``, the event drops silently: the only reason
those listings exist is propagation, and there's nothing to propagate.
The caller checks ``AssessmentResult.has_alertable`` for this.
"""

from collections import deque
from dataclasses import dataclass, field
from typing import Any

from openai import APIError, AsyncOpenAI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.config import settings
from catalyst_radar.logging import get_logger
from catalyst_radar.models.company import TrackedCompany
from catalyst_radar.models.entity import (
    CompanyEntity,
    EntityRelationship,
    EventAffectedCompany,
)
from catalyst_radar.models.event import Event
from catalyst_radar.prompt_context import current_date_line
from catalyst_radar.schemas.llm import AffectedCompaniesOutput
from catalyst_radar.services.openai_classifier import reasoning_off_for

log = get_logger(__name__)


# Edges traversed downward only (from → to); JV is bidirectional.
_DOWNWARD_KINDS = {"parent_of", "major_shareholder"}
_BIDIRECTIONAL_KINDS = {"joint_venture"}

_IMPORTANCE_RANK = {"low": 0, "medium": 1, "high": 2}


@dataclass(slots=True)
class _Candidate:
    """One entity reached by BFS that has at least one tracked listing.
    ``role`` is the label rendered on the Telegram card. ``hop_distance``
    is 0 for primary, 1 for direct parent/JV/shareholder, 2 for siblings
    (reached via a shared parent)."""

    entity_id: int
    entity_name: str
    entity_summary: str | None
    role: str
    hop_distance: int
    path: list[str]  # ["parent_of", "parent_of"] etc — for debugging
    tracked_listings: list[TrackedCompany] = field(default_factory=list)


@dataclass(slots=True)
class _LlmVerdict:
    include: bool
    importance: str
    reason: str


@dataclass(slots=True)
class AssessmentResult:
    """Summary of the propagation pass. The caller uses it to decide
    whether to drop the event silently (no primary-alertable + no
    affected) or to proceed with notification creation.

    ``payload_rows`` is the denormalized list the alert formatter reads
    from ``event.payload['affected']`` — keeps the Telegram render
    self-contained without an extra query at dispatch time."""

    primary_listings: list[TrackedCompany] = field(default_factory=list)
    affected_rows_inserted: int = 0
    candidates_evaluated: int = 0
    llm_status: str = "skipped"  # completed | failed | skipped | disabled
    llm_error: str | None = None
    has_alertable: bool = False
    payload_rows: list[dict[str, Any]] = field(default_factory=list)


_SYSTEM = (
    "You decide which of a trader's tracked companies are materially "
    "impacted by a specific catalyst news event. The primary company "
    "(the news subject) is already known; you assess each *related* "
    "candidate company that the user tracks via a corporate relationship "
    "(parent, subsidiary, sibling-subsidiary, JV partner, or major-"
    "shareholder linkage).\n"
    "\n"
    "For each candidate, return:\n"
    "- entity_ref: echo the candidate's identifier exactly as given.\n"
    "- include: true ONLY if this specific news would meaningfully move "
    "this candidate's share price within days. Be discerning — broad "
    "macro news, ESG reports, generic corporate-governance updates rarely "
    "cascade to subsidiaries. Concrete revenue / contract / regulatory / "
    "guidance / M&A news often does.\n"
    "- importance: low | medium | high. Use the candidate's own coupling "
    "to the news, not the primary's importance. A high-importance primary "
    "with weak coupling to the candidate → medium or low. A medium-importance "
    "primary tightly coupled to the candidate (e.g. flagship subsidiary in "
    "the affected segment) → medium or high.\n"
    "- reason: ONE plain English sentence (<= ~180 chars) citing a "
    "concrete fact from the news that explains why the candidate is or "
    "isn't impacted. NEVER guess; if the news content does not let you "
    "judge, set include=false with reason 'insufficient information in "
    "news to judge impact'.\n"
    "\n"
    "Whenever you reference a ticker, prefix with $. Write reasons in "
    "English even when the news is Chinese."
)


def _rank(v: str) -> int:
    return _IMPORTANCE_RANK.get(v.lower(), -1)


async def _load_entity_listings(
    session: AsyncSession, entity_ids: list[int]
) -> dict[int, list[TrackedCompany]]:
    if not entity_ids:
        return {}
    result = await session.execute(
        select(TrackedCompany)
        .where(TrackedCompany.entity_id.in_(entity_ids))
        .where(TrackedCompany.is_active.is_(True))
    )
    by_entity: dict[int, list[TrackedCompany]] = {eid: [] for eid in entity_ids}
    for tc in result.scalars().all():
        by_entity.setdefault(tc.entity_id, []).append(tc)
    return by_entity


async def _load_entity(session: AsyncSession, entity_id: int) -> CompanyEntity | None:
    return await session.get(CompanyEntity, entity_id)


async def _load_entities_bulk(
    session: AsyncSession, entity_ids: list[int]
) -> dict[int, CompanyEntity]:
    if not entity_ids:
        return {}
    result = await session.execute(
        select(CompanyEntity).where(CompanyEntity.id.in_(entity_ids))
    )
    return {e.id: e for e in result.scalars().all()}


async def _bfs_candidates(
    session: AsyncSession,
    *,
    primary_entity_id: int,
    max_hops: int,
) -> list[_Candidate]:
    """Walk the relationship graph and return candidate entities that
    have at least one active tracked listing. The primary entity is NOT
    in the output — it's handled separately as ``role=primary``.

    Multi-path: an entity reached by two paths keeps the lower
    hop_distance and the more-specific role. (Final dedup of
    ``effective_importance`` happens at LLM-output time, taking max.)"""
    edges_result = await session.execute(select(EntityRelationship))
    edges = list(edges_result.scalars().all())

    # Build an adjacency list per direction.
    forward: dict[int, list[tuple[int, str]]] = {}  # from → [(to, kind)]
    backward: dict[int, list[tuple[int, str]]] = {}  # to → [(from, kind)] (for JV reverse)
    for e in edges:
        forward.setdefault(e.from_entity_id, []).append((e.to_entity_id, e.kind))
        if e.kind in _BIDIRECTIONAL_KINDS:
            backward.setdefault(e.to_entity_id, []).append((e.from_entity_id, e.kind))

    # BFS — (current_entity_id, hop_distance, last_edge_kind, role, path).
    visited: set[int] = {primary_entity_id}
    discovered: dict[int, _Candidate] = {}
    queue: deque[tuple[int, int, str | None, list[str]]] = deque()
    queue.append((primary_entity_id, 0, None, []))

    while queue:
        node, hop, last_kind, path = queue.popleft()
        if hop >= max_hops:
            continue
        # Walk forward edges (parent_of / major_shareholder / JV).
        for nxt, kind in forward.get(node, []):
            if nxt in visited:
                continue
            if kind not in _DOWNWARD_KINDS and kind not in _BIDIRECTIONAL_KINDS:
                continue
            new_hop = hop + 1
            new_path = path + [f"{kind}→"]
            role = _role_for(path, kind, "forward")
            _record(discovered, nxt, role, new_hop, new_path)
            visited.add(nxt)
            queue.append((nxt, new_hop, kind, new_path))
        # Walk reverse JV edges.
        for nxt, kind in backward.get(node, []):
            if nxt in visited:
                continue
            new_hop = hop + 1
            new_path = path + [f"{kind}←"]
            role = _role_for(path, kind, "reverse")
            _record(discovered, nxt, role, new_hop, new_path)
            visited.add(nxt)
            queue.append((nxt, new_hop, kind, new_path))

    # Hydrate candidate entities + their tracked listings; drop entities
    # with no listings (nothing to alert). Bulk-load both to avoid N+1.
    entity_ids = list(discovered.keys())
    listings_by_entity = await _load_entity_listings(session, entity_ids)
    entities_by_id = await _load_entities_bulk(session, entity_ids)
    out: list[_Candidate] = []
    for eid, cand in discovered.items():
        listings = listings_by_entity.get(eid, [])
        if not listings:
            continue
        entity = entities_by_id.get(eid)
        if entity is None:
            continue
        cand.entity_name = entity.canonical_name
        cand.entity_summary = entity.summary
        cand.tracked_listings = listings
        out.append(cand)
    return out


def _role_for(prior_path: list[str], kind: str, direction: str) -> str:
    """Map the incoming edge to a display role.

    The role describes the candidate's IMMEDIATE relationship to the
    intermediary it was reached from — not to the primary. Hop distance
    is tracked separately on the affected row so the formatter can show
    "(indirect)" when it matters. This avoids the previous bug where
    every hop-2 path was labeled "sibling" regardless of the actual
    edge kind (a JV-partner-of-a-parent would be mis-rendered as
    sibling, misleading the trader about the structural relationship).
    """
    if kind == "parent_of" and direction == "forward":
        return "subsidiary"
    if kind == "major_shareholder" and direction == "forward":
        return "shareholder_of"
    if kind == "joint_venture":
        return "jv_partner"
    return "related"


def _record(
    discovered: dict[int, _Candidate],
    entity_id: int,
    role: str,
    hop_distance: int,
    path: list[str],
) -> None:
    existing = discovered.get(entity_id)
    if existing is None or hop_distance < existing.hop_distance:
        discovered[entity_id] = _Candidate(
            entity_id=entity_id,
            entity_name="",  # hydrated later
            entity_summary=None,
            role=role,
            hop_distance=hop_distance,
            path=path,
        )


def _entity_ref(candidate: _Candidate) -> str:
    """Stable ref echoed back by the LLM so we can join its verdict to
    the candidate. Includes hop+role so two candidates with the same
    name don't collide."""
    return f"entity:{candidate.entity_id}:{candidate.role}:h{candidate.hop_distance}"


def _build_assessment_user_message(
    *, event: Event, primary_summary: str | None, candidates: list[_Candidate]
) -> str:
    p = event.payload or {}
    cls = (p.get("classification") or {}) if isinstance(p, dict) else {}
    parts = [
        f"PRIMARY: {event.company_name or '(unknown)'} (${event.symbol or '?'})",
        f"Primary importance: {cls.get('importance') or 'unknown'}",
        f"Subtype: {cls.get('event_subtype') or 'unknown'}",
        f"Headline: {event.title or '(no title)'}",
        f"Summary: {cls.get('summary') or (p.get('news', {}) or {}).get('title') or ''}",
        f"Why it matters: {cls.get('why_it_matters') or ''}",
    ]
    if primary_summary:
        parts.append(f"Primary business: {primary_summary}")
    parts.append("")
    parts.append("CANDIDATES (assess each):")
    for cand in candidates:
        tickers = ", ".join(
            f"${tc.symbol}" for tc in cand.tracked_listings if tc.symbol
        )
        parts.append(
            f"- entity_ref={_entity_ref(cand)}\n"
            f"  name: {cand.entity_name}\n"
            f"  tickers: {tickers}\n"
            f"  role: {cand.role} (hop {cand.hop_distance})\n"
            f"  business: {cand.entity_summary or '(no summary on file)'}"
        )
    return "\n".join(parts)


_CACHED_CLIENT: AsyncOpenAI | None = None
_CACHED_CLIENT_KEY: tuple[str, int] | None = None


def _shared_assessment_client() -> AsyncOpenAI | None:
    """Module-level cached client so a catalyst sync run with many
    events doesn't open a fresh HTTPX connection pool per event. Keyed
    by (api_key, timeout) so a config change still picks up. Returns
    None when the API key is missing."""
    global _CACHED_CLIENT, _CACHED_CLIENT_KEY
    if not settings.openai_api_key:
        return None
    # Use the consensus timeout ceiling: the assessment fans the model
    # over up to N candidates with ~600-char summaries each, which can
    # genuinely take longer than openai_timeout_seconds (default 30 s).
    timeout = int(settings.catalyst_enrich_consensus_timeout_seconds)
    key = (settings.openai_api_key, timeout)
    if _CACHED_CLIENT is None or _CACHED_CLIENT_KEY != key:
        _CACHED_CLIENT = AsyncOpenAI(
            api_key=settings.openai_api_key, timeout=timeout
        )
        _CACHED_CLIENT_KEY = key
    return _CACHED_CLIENT


async def aclose_shared_assessment_client() -> None:
    """Close + clear the cached client. Called by tasks._run at the end
    of every Celery task, *inside* the task's event loop: the cache must
    not outlive the loop its connection pool was created on (Celery runs
    one fresh loop per task), and the SDK's GC-time fallback schedules
    aclose() on whatever loop happens to be running later — producing
    'Task exception was never retrieved' noise when that loop closes
    first. Within-run pooling (the reason this cache exists) is
    unaffected: one task == one run."""
    global _CACHED_CLIENT, _CACHED_CLIENT_KEY
    client = _CACHED_CLIENT
    _CACHED_CLIENT = None
    _CACHED_CLIENT_KEY = None
    if client is not None:
        await client.close()


async def _call_llm(
    *, event: Event, primary_summary: str | None, candidates: list[_Candidate]
) -> tuple[str, dict[str, _LlmVerdict] | None, str | None]:
    """Returns (status, verdicts_by_ref, error). Verdicts is None on
    failure. Refusals / non-completed responses become failed."""
    client = _shared_assessment_client()
    if client is None:
        return ("disabled", None, "OPENAI_API_KEY not configured")
    model = settings.relationship_assessment_model
    try:
        response = await client.responses.parse(
            model=model,
            input=[
                {"role": "system", "content": _SYSTEM + current_date_line()},
                {
                    "role": "user",
                    "content": _build_assessment_user_message(
                        event=event,
                        primary_summary=primary_summary,
                        candidates=candidates,
                    ),
                },
            ],
            text_format=AffectedCompaniesOutput,
            temperature=0,
            max_output_tokens=settings.relationship_assessment_max_output_tokens,
            reasoning=reasoning_off_for(model),
        )
    except APIError as exc:
        return ("failed", None, repr(exc))

    if response.status != "completed" or response.output_parsed is None:
        return (
            "failed",
            None,
            str(response.incomplete_details or response.status),
        )

    verdicts: dict[str, _LlmVerdict] = {}
    for a in response.output_parsed.assessments:
        verdicts[a.entity_ref] = _LlmVerdict(
            include=bool(a.include),
            importance=str(a.importance or "low").lower(),
            reason=(a.reason or "").strip(),
        )
    return ("completed", verdicts, None)


async def assess_affected_companies(
    session: AsyncSession,
    event: Event,
    *,
    primary_tracked: TrackedCompany,
    max_hops: int | None = None,
) -> AssessmentResult:
    """Run BFS + LLM assessment for one classified catalyst event.

    Writes ``event_affected_companies`` rows for every materially-
    impacted tracked listing (primary listings always; LLM-assessed
    candidates when ``include=true``). Multi-path duplicates dedup by
    ``max(effective_importance)``. The caller is expected to commit.

    ``primary_tracked`` is the tracked company whose feed produced the
    event — its ``entity_id`` is the BFS root. Every active listing
    sharing that entity_id becomes a primary row.
    """
    result = AssessmentResult()
    primary_entity_id = primary_tracked.entity_id
    if not primary_entity_id:
        # Old data path — entity layer not populated for this tracked.
        # Treat the tracked company as its own primary row at the
        # classifier's importance; skip propagation. Log loudly: this
        # is a data gap (an active tracked company should always have
        # an entity_id) and the user otherwise has no way to know
        # propagation is silently disabled for this row.
        log.warning(
            "spillover_skipped_missing_entity",
            tracked_company_id=primary_tracked.id,
            symbol=primary_tracked.symbol,
            event_id=event.id,
        )
        result.llm_status = "skipped"
        await _write_primary_only(session, event, primary_tracked, result)
        return result

    # Primary listings (every tracked listing that shares the entity).
    primary_listings = await _load_entity_listings(session, [primary_entity_id])
    primaries = primary_listings.get(primary_entity_id, [])
    if not primaries:
        primaries = [primary_tracked]  # defensive: include the originating tc

    # Primary summary (for LLM context).
    primary_entity = await _load_entity(session, primary_entity_id)
    primary_summary = primary_entity.summary if primary_entity is not None else None

    # BFS candidates (excluding the primary entity itself).
    hops = max_hops if max_hops is not None else int(settings.relationship_max_hops)
    candidates = await _bfs_candidates(
        session, primary_entity_id=primary_entity_id, max_hops=hops
    )
    # Drop candidates whose listings overlap a primary listing — happens
    # when a JV cycle bounces back to the primary entity via two edges.
    primary_listing_ids = {tc.id for tc in primaries}
    for cand in candidates:
        cand.tracked_listings = [
            tc for tc in cand.tracked_listings if tc.id not in primary_listing_ids
        ]
    candidates = [c for c in candidates if c.tracked_listings]
    result.candidates_evaluated = len(candidates)

    # Importance to assign to primary rows = the classifier's importance.
    primary_importance = _primary_importance(event)

    # Build the rows we will insert. Primary first (always), then LLM-
    # judged candidates.
    rows_by_tc: dict[int, EventAffectedCompany] = {}
    for tc in primaries:
        rows_by_tc[tc.id] = EventAffectedCompany(
            event_id=event.id,
            tracked_company_id=tc.id,
            role="primary",
            effective_importance=primary_importance,
            reason=None,
            hop_distance=0,
        )
        result.primary_listings.append(tc)

    # Call LLM for the candidates (if any).
    if candidates:
        status, verdicts, error = await _call_llm(
            event=event,
            primary_summary=primary_summary,
            candidates=candidates,
        )
        result.llm_status = status
        result.llm_error = error
        if verdicts is not None:
            for cand in candidates:
                v = verdicts.get(_entity_ref(cand))
                if v is None or not v.include:
                    continue
                for tc in cand.tracked_listings:
                    existing = rows_by_tc.get(tc.id)
                    if existing is not None:
                        # Multi-path: keep max importance, keep more-specific
                        # role if existing was a fallback.
                        if _rank(v.importance) > _rank(existing.effective_importance):
                            existing.effective_importance = v.importance
                            existing.reason = v.reason
                            existing.role = cand.role
                            existing.hop_distance = cand.hop_distance
                        continue
                    rows_by_tc[tc.id] = EventAffectedCompany(
                        event_id=event.id,
                        tracked_company_id=tc.id,
                        role=cand.role,
                        effective_importance=v.importance,
                        reason=v.reason,
                        hop_distance=cand.hop_distance,
                    )

    # Persist + denormalize.
    tc_by_id: dict[int, TrackedCompany] = {tc.id: tc for tc in primaries}
    for cand in candidates:
        for tc in cand.tracked_listings:
            tc_by_id.setdefault(tc.id, tc)
    for row in rows_by_tc.values():
        session.add(row)
    await session.flush()
    result.affected_rows_inserted = len(rows_by_tc)
    result.payload_rows = _denormalize_rows(rows_by_tc, tc_by_id)

    # Decide alertability: at least one non-parent-only primary OR at
    # least one non-primary affected listing.
    any_primary_alertable = any(
        not getattr(tc, "is_parent_only", False) for tc in primaries
    )
    any_affected = any(row.role != "primary" for row in rows_by_tc.values())
    result.has_alertable = any_primary_alertable or any_affected

    log.info(
        "affected_companies_assessed",
        event_id=event.id,
        primaries=len(primaries),
        candidates=result.candidates_evaluated,
        rows=result.affected_rows_inserted,
        llm_status=result.llm_status,
        alertable=result.has_alertable,
    )
    return result


async def _write_primary_only(
    session: AsyncSession,
    event: Event,
    tc: TrackedCompany,
    result: AssessmentResult,
) -> None:
    """Fallback when the primary tracked company has no entity_id
    populated yet (pre-migration data)."""
    row = EventAffectedCompany(
        event_id=event.id,
        tracked_company_id=tc.id,
        role="primary",
        effective_importance=_primary_importance(event),
        reason=None,
        hop_distance=0,
    )
    session.add(row)
    await session.flush()
    result.primary_listings.append(tc)
    result.affected_rows_inserted = 1
    result.has_alertable = not getattr(tc, "is_parent_only", False)
    result.payload_rows = _denormalize_rows({tc.id: row}, {tc.id: tc})


def _denormalize_rows(
    rows: dict[int, EventAffectedCompany],
    tc_by_id: dict[int, TrackedCompany],
) -> list[dict[str, Any]]:
    """Sort and serialize affected rows into the JSON-friendly form
    the alert formatter reads from ``event.payload['affected']``."""
    ordered = sorted(
        rows.values(),
        key=lambda r: (
            0 if r.role == "primary" else 1,
            -_rank(r.effective_importance),
            r.hop_distance,
        ),
    )
    out: list[dict[str, Any]] = []
    for r in ordered:
        tc = tc_by_id.get(r.tracked_company_id)
        out.append(
            {
                "tracked_company_id": r.tracked_company_id,
                "ticker": tc.symbol if tc else None,
                "exchange": tc.exchange if tc else None,
                "name": tc.company_name if tc else None,
                "is_parent_only": bool(getattr(tc, "is_parent_only", False)) if tc else False,
                "role": r.role,
                "importance": r.effective_importance,
                "reason": r.reason,
                "hop_distance": r.hop_distance,
            }
        )
    return out


def _primary_importance(event: Event) -> str:
    p = event.payload or {}
    cls = (p.get("classification") or {}) if isinstance(p, dict) else {}
    val = str(cls.get("importance") or "medium").lower()
    return val if val in {"low", "medium", "high"} else "medium"


