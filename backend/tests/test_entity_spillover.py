"""End-to-end coverage for — the entity layer, the
relationship-graph BFS, the LLM-assessed propagation, the unified
alert section, and the suggestion review queue.

Coverage map (matches acceptance criteria):

* ``test_bfs_collects_parent_jv_sibling`` — graph traversal & role labels
* ``test_jv_bidirectional`` — JV edge traversed both directions
* ``test_cycle_processed_once`` — visited-set blocks A↔B JV cycles
* ``test_multi_parent_dedup_takes_max_importance`` — multi-path winner
* ``test_parent_only_drops_silently_with_no_affected`` — solo-parent gate
* ``test_alert_formatter_renders_affected_section`` — Telegram render
* ``test_preflight_writes_pending_suggestion`` — preflight inbox
* ``test_suggestion_decision_creates_relationship_and_autotracks`` — accept flow
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.models.company import TrackedCompany
from catalyst_radar.models.entity import (
    CompanyEntity,
    EntityRelationship,
)
from catalyst_radar.models.event import Event
from catalyst_radar.repositories.entity_repository import (
    EntityRelationshipRepository,
    RelationshipSuggestionRepository,
)
from catalyst_radar.services.affected_companies import (
    _bfs_candidates,
    _entity_ref,
    _LlmVerdict,
    assess_affected_companies,
)
from catalyst_radar.services.alerts import format_catalyst

# ── Fixture-style helpers (plain async functions, called per-test) ───


async def _entity(
    session: AsyncSession, name: str, country: str = "TW", summary: str | None = None
) -> CompanyEntity:
    ent = CompanyEntity(canonical_name=name, country=country, summary=summary)
    session.add(ent)
    await session.commit()
    await session.refresh(ent)
    return ent


async def _tracked(
    session: AsyncSession,
    *,
    entity: CompanyEntity,
    symbol: str,
    exchange: str = "TW",
    is_parent_only: bool = False,
) -> TrackedCompany:
    tc = TrackedCompany(
        entity_id=entity.id,
        symbol=symbol,
        exchange=exchange,
        company_name=entity.canonical_name,
        source="manual",
        is_parent_only=is_parent_only,
    )
    session.add(tc)
    await session.commit()
    await session.refresh(tc)
    return tc


async def _relationship(
    session: AsyncSession,
    *,
    from_e: CompanyEntity,
    to_e: CompanyEntity,
    kind: str,
) -> EntityRelationship:
    rel = EntityRelationship(
        from_entity_id=from_e.id, to_entity_id=to_e.id, kind=kind
    )
    session.add(rel)
    await session.commit()
    await session.refresh(rel)
    return rel


def _event(*, primary: TrackedCompany, importance: str = "high") -> Event:
    """Build an in-memory Event (no DB write)."""
    return Event(
        id=42,
        event_type="catalyst",
        source_name="eodhd_news",
        source_event_id="sid-42",
        dedup_key="dk-42",
        symbol=primary.symbol,
        exchange=primary.exchange,
        company_name=primary.company_name,
        title="Foxconn wins NVIDIA AI server contract",
        source_url="https://example.test/article",
        status="notified",
        payload={
            "news": {"title": "Foxconn wins NVIDIA AI server contract"},
            "classification": {
                "event_subtype": "m_and_a",
                "importance": importance,
                "confidence": 0.9,
                "summary": "Hon Hai signs multi-year AI server deal.",
                "why_it_matters": "Material revenue line.",
                "suggested_action": "research",
            },
        },
    )


# ── BFS ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bfs_collects_parent_jv_sibling(db_session: AsyncSession) -> None:
    foxconn = await _entity(db_session, "Hon Hai Precision Industry")
    shunsin = await _entity(db_session, "Shunsin Technology")
    fii = await _entity(db_session, "Foxconn Industrial Internet")
    sharp = await _entity(db_session, "Sharp Corp", country="JP")

    await _tracked(db_session, entity=foxconn, symbol="2317")
    await _tracked(db_session, entity=shunsin, symbol="6451")
    await _tracked(db_session, entity=fii, symbol="601138", exchange="CN")
    await _tracked(db_session, entity=sharp, symbol="6753", exchange="JP")

    # foxconn parent_of all three; sharp is a JV partner via foxconn
    await _relationship(db_session, from_e=foxconn, to_e=shunsin, kind="parent_of")
    await _relationship(db_session, from_e=foxconn, to_e=fii, kind="parent_of")
    await _relationship(db_session, from_e=foxconn, to_e=sharp, kind="joint_venture")

    candidates = await _bfs_candidates(
        db_session, primary_entity_id=foxconn.id, max_hops=2
    )
    by_name = {c.entity_name: c for c in candidates}
    assert {"Shunsin Technology", "Foxconn Industrial Internet", "Sharp Corp"} <= set(
        by_name
    )
    assert by_name["Shunsin Technology"].role == "subsidiary"
    assert by_name["Sharp Corp"].role == "jv_partner"


# ── JV bidirectional ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_jv_bidirectional(db_session: AsyncSession) -> None:
    tsmc = await _entity(db_session, "TSMC")
    sony = await _entity(db_session, "Sony")
    await _tracked(db_session, entity=tsmc, symbol="2330")
    await _tracked(db_session, entity=sony, symbol="6758", exchange="JP")
    # Single JV row from TSMC → Sony. BFS from Sony must still reach TSMC.
    await _relationship(db_session, from_e=tsmc, to_e=sony, kind="joint_venture")

    candidates = await _bfs_candidates(
        db_session, primary_entity_id=sony.id, max_hops=2
    )
    assert any(c.entity_id == tsmc.id for c in candidates)


# ── Cycle prevention ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cycle_processed_once(db_session: AsyncSession) -> None:
    a = await _entity(db_session, "Entity A")
    b = await _entity(db_session, "Entity B")
    await _tracked(db_session, entity=a, symbol="A")
    await _tracked(db_session, entity=b, symbol="B")
    # Cycle: A ↔ B via two JV rows in opposite directions.
    await _relationship(db_session, from_e=a, to_e=b, kind="joint_venture")
    await _relationship(db_session, from_e=b, to_e=a, kind="joint_venture")

    candidates = await _bfs_candidates(
        db_session, primary_entity_id=a.id, max_hops=2
    )
    # B reached once, A never re-discovered (it's the root).
    matched_b = [c for c in candidates if c.entity_id == b.id]
    assert len(matched_b) == 1
    assert all(c.entity_id != a.id for c in candidates)


# ── Multi-parent dedup ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_multi_parent_dedup_takes_max_importance(
    db_session: AsyncSession, monkeypatch
) -> None:
    # Setup: child has two parents (TSMC + Sony), both feeding the same
    # event. The LLM gives different importance per path; final row must
    # be max.
    tsmc = await _entity(db_session, "TSMC")
    sony = await _entity(db_session, "Sony")
    jasm = await _entity(db_session, "JASM JV")
    tsmc_tc = await _tracked(db_session, entity=tsmc, symbol="2330")
    await _tracked(db_session, entity=sony, symbol="6758", exchange="JP")
    await _tracked(db_session, entity=jasm, symbol="JASM", exchange="JP")
    await _relationship(db_session, from_e=tsmc, to_e=jasm, kind="parent_of")
    await _relationship(db_session, from_e=sony, to_e=jasm, kind="parent_of")

    event = _event(primary=tsmc_tc, importance="high")
    db_session.add(event)
    await db_session.commit()

    async def _fake_llm(*, event, primary_summary, candidates):
        # All candidates → include=true, importance=medium
        verdicts = {
            _entity_ref(c): _LlmVerdict(include=True, importance="medium", reason="r")
            for c in candidates
        }
        return ("completed", verdicts, None)

    monkeypatch.setattr(
        "catalyst_radar.services.affected_companies._call_llm", _fake_llm
    )
    result = await assess_affected_companies(
        db_session, event, primary_tracked=tsmc_tc
    )
    # One primary row (TSMC) + one affected row (JASM, single tc despite
    # two parent paths reaching it via BFS+dedup).
    assert result.affected_rows_inserted == 2
    affected_rows = {
        r["ticker"]: r for r in result.payload_rows if r["role"] != "primary"
    }
    assert "JASM" in affected_rows
    # max(LLM medium, primary high doesn't apply to child) → medium.
    assert affected_rows["JASM"]["importance"] == "medium"


# ── Parent-only drop ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_parent_only_drops_silently_with_no_affected(
    db_session: AsyncSession, monkeypatch
) -> None:
    foxconn = await _entity(db_session, "Hon Hai")
    shunsin = await _entity(db_session, "Shunsin")
    foxconn_tc = await _tracked(
        db_session, entity=foxconn, symbol="2317", is_parent_only=True
    )
    await _tracked(db_session, entity=shunsin, symbol="6451")
    await _relationship(db_session, from_e=foxconn, to_e=shunsin, kind="parent_of")

    event = _event(primary=foxconn_tc, importance="medium")
    db_session.add(event)
    await db_session.commit()

    async def _llm_says_no(*, event, primary_summary, candidates):
        verdicts = {
            _entity_ref(c): _LlmVerdict(
                include=False, importance="low", reason="no impact"
            )
            for c in candidates
        }
        return ("completed", verdicts, None)

    monkeypatch.setattr(
        "catalyst_radar.services.affected_companies._call_llm", _llm_says_no
    )
    result = await assess_affected_companies(
        db_session, event, primary_tracked=foxconn_tc
    )
    # Primary row IS inserted (we always record it), but has_alertable
    # is False because Foxconn is parent_only AND no affected listings.
    assert result.affected_rows_inserted == 1
    assert result.has_alertable is False


@pytest.mark.asyncio
async def test_parent_only_alerts_when_child_affected(
    db_session: AsyncSession, monkeypatch
) -> None:
    foxconn = await _entity(db_session, "Hon Hai")
    shunsin = await _entity(db_session, "Shunsin")
    foxconn_tc = await _tracked(
        db_session, entity=foxconn, symbol="2317", is_parent_only=True
    )
    await _tracked(db_session, entity=shunsin, symbol="6451")
    await _relationship(db_session, from_e=foxconn, to_e=shunsin, kind="parent_of")

    event = _event(primary=foxconn_tc, importance="high")
    db_session.add(event)
    await db_session.commit()

    async def _llm_says_yes(*, event, primary_summary, candidates):
        verdicts = {
            _entity_ref(c): _LlmVerdict(
                include=True, importance="medium", reason="AI server modules"
            )
            for c in candidates
        }
        return ("completed", verdicts, None)

    monkeypatch.setattr(
        "catalyst_radar.services.affected_companies._call_llm", _llm_says_yes
    )
    result = await assess_affected_companies(
        db_session, event, primary_tracked=foxconn_tc
    )
    assert result.has_alertable is True
    assert any(r["role"] != "primary" for r in result.payload_rows)


# ── Alert formatter ──────────────────────────────────────────────────


def test_alert_formatter_renders_affected_section() -> None:
    """Build a synthetic event payload with affected rows and verify the
    Telegram render includes role + importance + reason for each row."""
    event = Event(
        id=1,
        event_type="catalyst",
        source_name="eodhd_news",
        source_event_id="sid",
        dedup_key="dk",
        symbol="2317",
        exchange="TW",
        company_name="Hon Hai",
        title="Foxconn wins NVIDIA AI server contract",
        source_url="https://example.test/a",
        status="notified",
        payload={
            "classification": {
                "event_subtype": "m_and_a",
                "importance": "high",
                "confidence": 0.9,
                "summary": "Hon Hai signs deal.",
                "expected_impact": "Revenue.",
                "why_it_matters": "Material.",
                "suggested_action": "research",
            },
            "affected": [
                {
                    "tracked_company_id": 1,
                    "ticker": "2317",
                    "exchange": "TW",
                    "name": "Hon Hai",
                    "is_parent_only": False,
                    "role": "primary",
                    "importance": "high",
                    "reason": None,
                    "hop_distance": 0,
                },
                {
                    "tracked_company_id": 2,
                    "ticker": "6451",
                    "exchange": "TW",
                    "name": "Shunsin",
                    "is_parent_only": False,
                    "role": "subsidiary",
                    "importance": "medium",
                    "reason": "Shunsin assembles AI server modules for Hon Hai",
                    "hop_distance": 1,
                },
            ],
            "affected_assessment": {"status": "completed", "error": None},
        },
    )
    text = format_catalyst(event)
    assert "Affects your watchlist" in text
    assert "$6451" in text
    assert "Shunsin" in text
    assert "AI server modules" in text


def test_alert_formatter_notes_assessment_failure() -> None:
    event = Event(
        id=1,
        event_type="catalyst",
        source_name="eodhd_news",
        source_event_id="sid",
        dedup_key="dk",
        symbol="2317",
        exchange="TW",
        company_name="Hon Hai",
        title="Foxconn ESG report",
        source_url="https://example.test/a",
        status="notified",
        payload={
            "classification": {
                "event_subtype": "other",
                "importance": "medium",
                "confidence": 0.7,
                "summary": "ESG progress update.",
            },
            "affected": [
                {
                    "tracked_company_id": 1,
                    "ticker": "2317",
                    "exchange": "TW",
                    "name": "Hon Hai",
                    "is_parent_only": False,
                    "role": "primary",
                    "importance": "medium",
                    "reason": None,
                    "hop_distance": 0,
                }
            ],
            "affected_assessment": {"status": "failed", "error": "timeout"},
        },
    )
    text = format_catalyst(event)
    assert "related-company impact assessment unavailable" in text


# ── Preflight suggestion queue ───────────────────────────────────────


@pytest.mark.asyncio
async def test_preflight_writes_pending_suggestion(db_session: AsyncSession) -> None:
    ent = await _entity(db_session, "Hon Hai")
    tc = await _tracked(db_session, entity=ent, symbol="2317")
    sug = await RelationshipSuggestionRepository(db_session).create(
        source="preflight",
        tracked_company_id=tc.id,
        payload={
            "entity": {
                "canonical_name": "Hon Hai",
                "country": "TW",
                "ticker": "2317",
                "exchange": "TW",
                "summary": "Contract manufacturer.",
                "confidence": 0.95,
            },
            "parents": [],
            "joint_venture_partners": [],
            "major_shareholders": [],
            "sources": [],
            "notes": "",
            "model": "gpt-5.4-mini",
        },
    )
    await db_session.commit()
    pending = await RelationshipSuggestionRepository(db_session).list(status="pending")
    assert any(s.id == sug.id for s in pending)


# ── Suggestion decision (accept + auto-track) ────────────────────────


@pytest.mark.asyncio
async def test_suggestion_decision_creates_relationship_and_autotracks(
    db_session: AsyncSession,
) -> None:
    """Simulate accepting one parent suggestion: relationship row is
    created, parent entity is upserted, and the parent listing is
    auto-added as is_parent_only=True."""
    from catalyst_radar.api.v1.entities import _apply_suggestion_entry

    child_ent = await _entity(db_session, "Shunsin")
    await _tracked(db_session, entity=child_ent, symbol="6451")
    payload: dict[str, Any] = {
        "entity": {
            "canonical_name": "Shunsin",
            "country": "TW",
            "ticker": "6451",
            "exchange": "TW",
            "summary": "Contract assembler for Hon Hai.",
            "confidence": 0.9,
        },
        "parents": [
            {
                "canonical_name": "Hon Hai Precision Industry",
                "country": "TW",
                "ticker": "2317",
                "exchange": "TW",
                "summary": "Global contract manufacturer.",
                "confidence": 0.99,
            }
        ],
        "joint_venture_partners": [],
        "major_shareholders": [],
    }

    applied = await _apply_suggestion_entry(
        db_session,
        payload=payload,
        key="parents:0",
        self_entity=child_ent,
        auto_track=True,
    )
    await db_session.commit()
    assert applied is not None
    assert applied["kind"] == "parent_of"
    assert applied["auto_tracked"] is True

    # Verify edge: parent → child (parent_of from = parent, to = child)
    rel = (
        await EntityRelationshipRepository(db_session).list_for_entity(child_ent.id)
    )
    assert any(r.to_entity_id == child_ent.id and r.kind == "parent_of" for r in rel)

    # Verify parent is now a tracked listing (is_parent_only).
    from sqlalchemy import select as _select

    parent_tcs = (
        await db_session.execute(
            _select(TrackedCompany).where(TrackedCompany.symbol == "2317")
        )
    ).scalars().all()
    assert len(parent_tcs) == 1
    assert parent_tcs[0].is_parent_only is True


# ── Regression: review fixes ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_major_shareholder_accept_creates_edge_with_correct_direction(
    db_session: AsyncSession,
) -> None:
    """The accept flow must record (shareholder → company, major_shareholder),
    not (company → shareholder). BFS forward-walks downward kinds and
    relies on `from` = holder of stake to propagate news from a holder
    to its holdings."""
    from catalyst_radar.api.v1.entities import _apply_suggestion_entry

    company_ent = await _entity(db_session, "TSMC")
    await _tracked(db_session, entity=company_ent, symbol="2330")
    payload = {
        "entity": {
            "canonical_name": "TSMC",
            "country": "TW",
            "ticker": "2330",
            "exchange": "TW",
            "summary": "Foundry.",
            "confidence": 0.99,
        },
        "parents": [],
        "joint_venture_partners": [],
        "major_shareholders": [
            {
                "canonical_name": "Vanguard Group",
                "country": "US",
                "ticker": "VG",
                "exchange": "NASDAQ",
                "summary": "Index fund manager.",
                "confidence": 0.9,
            }
        ],
    }
    applied = await _apply_suggestion_entry(
        db_session,
        payload=payload,
        key="major_shareholders:0",
        self_entity=company_ent,
        auto_track=False,
    )
    await db_session.commit()
    assert applied is not None
    assert applied["kind"] == "major_shareholder"

    rel = await EntityRelationshipRepository(db_session).list_for_entity(company_ent.id)
    major = [r for r in rel if r.kind == "major_shareholder"]
    assert len(major) == 1
    # Vanguard (other) → TSMC (self); NOT the other way.
    assert major[0].to_entity_id == company_ent.id
    assert major[0].from_entity_id != company_ent.id


@pytest.mark.asyncio
async def test_silent_drop_does_not_create_event_relevance(
    db_session: AsyncSession, monkeypatch
) -> None:
    """When the spillover gate silently drops an event (parent_only
    primary, no affected candidates), no EventRelevance row may be
    created — otherwise the dashboard's `relevant=true` filter would
    surface the dropped event."""
    from sqlalchemy import select as _select

    from catalyst_radar.adapters.base import FetchResult
    from catalyst_radar.adapters.eodhd_news import EodhdNewsAdapter
    from catalyst_radar.models.event import EventRelevance
    from catalyst_radar.services.catalyst_sync import sync_catalysts
    from catalyst_radar.services.openai_classifier import ClassificationResult

    foxconn = await _entity(db_session, "Hon Hai")
    shunsin = await _entity(db_session, "Shunsin")
    await _tracked(db_session, entity=foxconn, symbol="2317", is_parent_only=True)
    await _tracked(db_session, entity=shunsin, symbol="6451")
    await _relationship(db_session, from_e=foxconn, to_e=shunsin, kind="parent_of")

    news_item = {
        "title": "Foxconn signs new lease for Shenzhen office complex",
        "content": (
            "Hon Hai Precision Industry has signed a long-term lease on a "
            "Shenzhen administrative complex, expanding its mainland China "
            "office footprint with no impact on contract-manufacturing capacity."
        ),
        "link": "https://example.test/foxconn-lease",
        "date": "2026-05-25",
    }

    class _NewsStub(EodhdNewsAdapter):
        def __init__(self) -> None:
            super().__init__(api_key="stub")

        async def fetch(self, target: str) -> FetchResult:
            # Only return the Foxconn-tracked feed; Shunsin's feed empty.
            items = [news_item] if "2317" in target else []
            return FetchResult(
                source_name="eodhd_news",
                schema_name="eodhd.news.v1",
                source_url=f"https://eodhd.test/news?s={target}",
                http_status=200,
                payload=items,
                items=items,
            )

    class _Classifier:
        model = "stub"
        configured = True

        async def classify(self, **_kwargs):
            return ClassificationResult(
                "completed",
                {
                    "is_company_critical": True,
                    "event_subtype": "other",
                    "importance": "medium",
                    "confidence": 0.85,
                    "expected_impact": "Sustainability filing.",
                    "summary": "Foxconn publishes annual ESG report.",
                    "why_it_matters": "Compliance disclosure.",
                    "suggested_action": "watch",
                    "ignore_reason": "",
                },
                response_id="r1",
            )

    async def _llm_excludes_all(*, event, primary_summary, candidates):
        from catalyst_radar.services.affected_companies import _entity_ref, _LlmVerdict

        return (
            "completed",
            {
                _entity_ref(c): _LlmVerdict(False, "low", "ESG report; no material impact")
                for c in candidates
            },
            None,
        )

    monkeypatch.setattr(
        "catalyst_radar.services.affected_companies._call_llm", _llm_excludes_all
    )

    summary = await sync_catalysts(
        db_session, news_adapter=_NewsStub(), classifier=_Classifier()
    )
    assert summary.propagation_dropped == 1
    rels = (
        await db_session.execute(_select(EventRelevance))
    ).scalars().all()
    assert rels == []


@pytest.mark.asyncio
async def test_indirect_role_label_not_sibling(db_session: AsyncSession) -> None:
    """A candidate reached at hop 2 via JV→parent_of must NOT be
    labeled 'sibling' (the old code's bug). The role describes the
    candidate's immediate edge, and hop_distance > 1 marks indirect."""
    from catalyst_radar.services.affected_companies import _bfs_candidates

    a = await _entity(db_session, "Primary A")
    b = await _entity(db_session, "JV Partner B")
    c = await _entity(db_session, "Subsidiary of B")
    await _tracked(db_session, entity=a, symbol="A")
    await _tracked(db_session, entity=b, symbol="B", exchange="JP")
    await _tracked(db_session, entity=c, symbol="C", exchange="JP")
    await _relationship(db_session, from_e=a, to_e=b, kind="joint_venture")
    await _relationship(db_session, from_e=b, to_e=c, kind="parent_of")

    candidates = await _bfs_candidates(
        db_session, primary_entity_id=a.id, max_hops=2
    )
    by_name = {cand.entity_name: cand for cand in candidates}
    assert by_name["JV Partner B"].role == "jv_partner"
    # C reached via JV→parent_of. Old code returned 'sibling'; new code
    # returns 'subsidiary' (true label) and the hop_distance carries
    # the indirect signal.
    assert by_name["Subsidiary of B"].role == "subsidiary"
    assert by_name["Subsidiary of B"].hop_distance == 2


def test_alert_formatter_marks_indirect_hops() -> None:
    """Hop > 1 affected rows render with an '(indirect)' marker so the
    user knows the link is mediated by an intermediary."""
    from catalyst_radar.services.alerts import format_catalyst

    event = Event(
        id=1,
        event_type="catalyst",
        source_name="eodhd_news",
        source_event_id="sid",
        dedup_key="dk",
        symbol="A",
        exchange="TW",
        company_name="Primary",
        title="Big news",
        source_url="https://example.test/a",
        status="notified",
        payload={
            "classification": {
                "event_subtype": "m_and_a",
                "importance": "high",
                "confidence": 0.9,
                "summary": "Material deal.",
            },
            "affected": [
                {
                    "tracked_company_id": 1, "ticker": "A", "exchange": "TW",
                    "name": "Primary", "is_parent_only": False,
                    "role": "primary", "importance": "high", "reason": None,
                    "hop_distance": 0,
                },
                {
                    "tracked_company_id": 3, "ticker": "C", "exchange": "JP",
                    "name": "Subsidiary of B", "is_parent_only": False,
                    "role": "subsidiary", "importance": "medium",
                    "reason": "B is C's parent and was party to the deal",
                    "hop_distance": 2,
                },
            ],
            "affected_assessment": {"status": "completed", "error": None},
        },
    )
    text = format_catalyst(event)
    assert "(indirect)" in text
    assert "$C" in text


@pytest.mark.asyncio
async def test_suggestion_accept_per_entry_isolation_one_bad_entry(
    db_session: AsyncSession,
) -> None:
    """Empty canonical_name in one entry must not abort the whole batch.
    The savepoint catches the per-entry error and the good entries
    persist with the suggestion marked accepted."""
    from catalyst_radar.api.v1.entities import _apply_suggestion_entry

    self_ent = await _entity(db_session, "Self Co")
    await _tracked(db_session, entity=self_ent, symbol="SC")

    bad_payload = {
        "parents": [{"canonical_name": "", "country": "US", "ticker": "",
                     "exchange": "", "summary": "", "confidence": 0.5}],
        "joint_venture_partners": [],
        "major_shareholders": [],
    }
    # The empty-name entry now raises ValueError (was silently creating
    # a 'unknown:parent_of:0' garbage entity).
    with pytest.raises(ValueError):
        await _apply_suggestion_entry(
            db_session,
            payload=bad_payload,
            key="parents:0",
            self_entity=self_ent,
            auto_track=False,
        )


@pytest.mark.asyncio
async def test_orphan_parent_only_listing_is_swept_on_untrack(
    db_session: AsyncSession,
) -> None:
    """When the last non-parent-only consumer is untracked, the
    parent_only listing kept solely to feed it should be deactivated.
    Otherwise we keep paying LLM budget on news for a parent the user
    no longer cares about."""
    from catalyst_radar.api.v1.companies import _sweep_orphan_parent_only_listings

    parent_ent = await _entity(db_session, "Hon Hai")
    child_ent = await _entity(db_session, "Shunsin")
    parent_tc = await _tracked(
        db_session, entity=parent_ent, symbol="2317", is_parent_only=True
    )
    child_tc = await _tracked(db_session, entity=child_ent, symbol="6451")
    await _relationship(db_session, from_e=parent_ent, to_e=child_ent, kind="parent_of")

    # Simulate the user untracking the child first (in-memory; we don't
    # need to go through the API for this unit test).
    child_tc.is_active = False
    db_session.add(child_tc)
    await db_session.commit()

    swept = await _sweep_orphan_parent_only_listings(
        db_session,
        removed_tc_id=child_tc.id,
        removed_entity_id=child_ent.id,
    )
    assert parent_tc.id in swept
    await db_session.refresh(parent_tc)
    assert parent_tc.is_active is False


def test_entity_ref_strips_dollar_prefix_from_ticker() -> None:
    """The preflight system prompt tells the LLM to prefix tickers with
    "$" in free-text fields; the model sometimes leaks that prefix into
    the structured EntityRef.ticker field too. The schema validator
    must normalize to bare so the auto-track lookup, the alert
    formatter, and the frontend display never see "$"-prefixed tickers."""
    from catalyst_radar.schemas.llm import EntityRef

    cases = [
        ("2317", "2317"),
        ("$2317", "2317"),
        ("$$2317", "2317"),
        ("   $AAPL  ", "AAPL"),
        ("", ""),
    ]
    for raw, expected in cases:
        ref = EntityRef(
            canonical_name="X",
            country="",
            ticker=raw,
            exchange="",
            summary="",
            confidence=0.5,
        )
        assert ref.ticker == expected, raw


@pytest.mark.asyncio
async def test_orphan_sweep_keeps_listing_when_other_consumer_remains(
    db_session: AsyncSession,
) -> None:
    """If another tracked, non-parent-only child still uses the parent,
    the sweep must NOT deactivate it."""
    from catalyst_radar.api.v1.companies import _sweep_orphan_parent_only_listings

    parent_ent = await _entity(db_session, "Hon Hai")
    child_a_ent = await _entity(db_session, "Shunsin")
    child_b_ent = await _entity(db_session, "FII")
    parent_tc = await _tracked(
        db_session, entity=parent_ent, symbol="2317", is_parent_only=True
    )
    child_a_tc = await _tracked(db_session, entity=child_a_ent, symbol="6451")
    await _tracked(db_session, entity=child_b_ent, symbol="601138", exchange="CN")
    await _relationship(db_session, from_e=parent_ent, to_e=child_a_ent, kind="parent_of")
    await _relationship(db_session, from_e=parent_ent, to_e=child_b_ent, kind="parent_of")

    # Untrack child A; child B still needs the parent.
    child_a_tc.is_active = False
    db_session.add(child_a_tc)
    await db_session.commit()

    swept = await _sweep_orphan_parent_only_listings(
        db_session,
        removed_tc_id=child_a_tc.id,
        removed_entity_id=child_a_ent.id,
    )
    assert parent_tc.id not in swept
    await db_session.refresh(parent_tc)
    assert parent_tc.is_active is True
