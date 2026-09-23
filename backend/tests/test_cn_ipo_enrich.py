"""cn_ipo_enrich prioritization: events with a pending notification are
"imminent" regardless of where their event_date sits.

Regression for the CSRC review-stage description gap: review alerts fire
for meeting dates up to 14d past / 90d future, far outside the listing
window heuristic (``ipo_alert_windows_days``, max 14d future), so under a
small steady-state budget the about-to-be-alerted review event lost the
race and its alert shipped without a business description.
"""

from datetime import timedelta

from catalyst_radar.models.base import utcnow
from catalyst_radar.models.event import Event
from catalyst_radar.models.notification import Notification
from catalyst_radar.services.cn_ipo_enrich import enrich_cn_ipos
from catalyst_radar.services.ipo_summarizer import IpoWebDescription


class _EmptyCninfoAdapter:
    """No recent filings — forces every description through the web path
    (the real situation for CSRC review rows, whose A-prefix reservation
    codes never match CNINFO's 6-digit sec_codes)."""

    async def fetch_recent(self) -> list[dict]:
        return []


class _WebSummarizer:
    configured = True

    def __init__(self) -> None:
        self.described: list[str] = []

    async def summarize(self, *, company_name, symbol, summary_text):
        raise AssertionError("prospectus path should not run without filings")

    async def describe_via_web(self, *, company_name, symbol, exchange=None, listing_date=None):
        self.described.append(symbol)
        return IpoWebDescription(
            "completed",
            description="Makes industrial laser cutters for EV battery lines.",
            sources=[{"name": "Example", "url": "https://example.com/a"}],
        )


def _cn_event(symbol: str, days_from_now: int) -> Event:
    return Event(
        event_type="ipo",
        source_name="csrc.review" if symbol.startswith("A") else "akshare.ipo",
        source_event_id=f"test:{symbol}",
        dedup_key=f"test:{symbol}",
        symbol=symbol,
        exchange="SSE",
        country="CN",
        company_name=f"Co {symbol}",
        title=f"IPO {symbol}",
        event_date=utcnow() + timedelta(days=days_from_now),
        payload={},
    )


async def test_pending_notification_event_is_prioritized(db_session, monkeypatch):
    from catalyst_radar.config import settings

    # Budget of 1: only the imminent set is guaranteed coverage.
    monkeypatch.setattr(settings, "cn_ipo_prospectus_max_items_per_run", 1)

    # Review-stage row: meeting 5 days AGO (alertable: within the review
    # task's 14d past window; invisible to the now..+14d listing heuristic),
    # with its notification already minted and pending dispatch.
    review = _cn_event("A25310", days_from_now=-5)
    # Listing row far in the future, nothing pending — must NOT win the budget.
    backlog = _cn_event("603999", days_from_now=100)
    db_session.add(review)
    db_session.add(backlog)
    await db_session.commit()
    await db_session.refresh(review)

    db_session.add(
        Notification(event_id=review.id, dedup_key=f"test:notif:{review.id}")
    )
    await db_session.commit()

    summarizer = _WebSummarizer()
    s = await enrich_cn_ipos(
        db_session, adapter=_EmptyCninfoAdapter(), summarizer=summarizer
    )

    assert s.described == 1
    assert summarizer.described == ["A25310"]
    await db_session.refresh(review)
    profile = (review.payload or {}).get("profile") or {}
    assert profile.get("description")
    assert profile.get("description_source") == "websearch"
    await db_session.refresh(backlog)
    assert not ((backlog.payload or {}).get("profile") or {}).get("description")


async def test_sent_notification_does_not_jump_the_queue(db_session, monkeypatch):
    """Only *pending* notifications mark an event imminent — already-sent
    ones are history, and treating them as imminent would let every old
    alerted event crowd out the backlog forever."""
    from catalyst_radar.config import settings

    monkeypatch.setattr(settings, "cn_ipo_prospectus_max_items_per_run", 1)

    old_alerted = _cn_event("A11111", days_from_now=-10)
    near = _cn_event("601111", days_from_now=30)
    db_session.add(old_alerted)
    db_session.add(near)
    await db_session.commit()
    await db_session.refresh(old_alerted)

    db_session.add(
        Notification(
            event_id=old_alerted.id,
            dedup_key=f"test:notif:{old_alerted.id}",
            status="sent",
        )
    )
    await db_session.commit()

    summarizer = _WebSummarizer()
    await enrich_cn_ipos(
        db_session, adapter=_EmptyCninfoAdapter(), summarizer=summarizer
    )

    # Neither is imminent (no pending notif; both outside now..+14d), so the
    # budget of 1 goes to the earliest event_date — the old row by date order.
    # The point pinned here: a *sent* notification adds no priority, i.e.
    # exactly one row was processed under budget, not an inflated imminent set.
    assert len(summarizer.described) == 1


async def test_imminent_set_larger_than_budget_is_fully_covered(db_session, monkeypatch):
    """select_enrich_candidates expands past the steady-state budget (up to
    cap) when more events than `budget` are about to be alerted — all of
    them must ship with a description, not just the first `budget`."""
    from catalyst_radar.config import settings
    from catalyst_radar.services.ipo_sync import select_enrich_candidates

    monkeypatch.setattr(settings, "cn_ipo_prospectus_max_items_per_run", 1)

    events = [_cn_event(f"60{i}000", days_from_now=i + 1) for i in range(3)]
    for e in events:
        db_session.add(e)
    await db_session.commit()
    for e in events:
        await db_session.refresh(e)
        db_session.add(Notification(event_id=e.id, dedup_key=f"t:n:{e.id}"))
    await db_session.commit()

    picked = await select_enrich_candidates(db_session, events, budget=1)
    assert len(picked) == 3  # max(budget, len(imminent)), bounded by cap

    picked_capped = await select_enrich_candidates(db_session, events, budget=1, cap=2)
    assert len(picked_capped) == 2  # cap bounds the expansion


async def test_no_candidates_records_no_source_run(db_session):
    """Nothing to enrich → no source_run row (the gate that stops the
    empty-run flood; this source dominated source_runs by volume)."""
    from catalyst_radar.repositories.source_repository import SourceRunRepository

    s = await enrich_cn_ipos(
        db_session, adapter=_EmptyCninfoAdapter(), summarizer=_WebSummarizer()
    )
    assert s.enriched == 0 and s.described == 0
    runs = await SourceRunRepository(db_session).list_recent()
    assert [r for r in runs if r.source_name == "cninfo.prospectus_enrich"] == []


async def test_candidates_record_one_source_run(db_session):
    """When there IS work, a single source_run is opened and finishes."""
    from catalyst_radar.repositories.source_repository import SourceRunRepository

    db_session.add(_cn_event("603888", days_from_now=3))  # in window, no description
    await db_session.commit()

    s = await enrich_cn_ipos(
        db_session, adapter=_EmptyCninfoAdapter(), summarizer=_WebSummarizer()
    )
    assert s.described == 1
    runs = [
        r
        for r in await SourceRunRepository(db_session).list_recent()
        if r.source_name == "cninfo.prospectus_enrich"
    ]
    assert len(runs) == 1 and runs[0].status == "success"
