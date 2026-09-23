from datetime import date, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel

from catalyst_radar.api.deps import CurrentUser, SessionDep
from catalyst_radar.models.base import utcnow
from catalyst_radar.models.notification import Notification
from catalyst_radar.repositories.event_repository import EventRepository
from catalyst_radar.repositories.notification_repository import NotificationRepository

# Grace window between a user clicking Send and the dispatcher being
# allowed to ship the message. Must be greater than the toast's 5s undo
# timer so an undo within the window beats the dispatcher even if a beat
# tick lands inside it.
_SEND_DISPATCH_GRACE = timedelta(seconds=6)

router = APIRouter(prefix="/events", tags=["events"])

FeedbackLabel = Literal["useful", "not_useful", "false_positive", "false_negative"]

DecisionAction = Literal["send", "ignore", "promote", "restore_to_review", "restore_to_ignored"]


class EventOut(BaseModel):
    id: int
    event_type: str
    symbol: str | None
    exchange: str | None
    country: str | None
    company_name: str | None
    title: str | None
    event_date: datetime | None
    status: str
    source_url: str | None
    payload: dict[str, Any] | None
    created_at: datetime

    model_config = {"from_attributes": True}


class FeedbackRequest(BaseModel):
    # Literal so Pydantic rejects unknown labels (422) and the OpenAPI
    # schema documents the accepted values.
    label: FeedbackLabel


class PaginatedEvents(BaseModel):
    rows: list[EventOut]
    total: int
    limit: int
    offset: int


@router.get("", response_model=PaginatedEvents)
async def list_events(
    current_user: CurrentUser,
    session: SessionDep,
    event_type: Annotated[str | None, Query()] = None,
    relevant: Annotated[bool, Query()] = False,
    period: Annotated[Literal["all", "upcoming", "past"], Query()] = "all",
    date_from: Annotated[date | None, Query()] = None,
    date_to: Annotated[date | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PaginatedEvents:
    repo = EventRepository(session)
    filters = {
        "event_type": event_type,
        "relevant": relevant,
        "period": period,
        "date_from": date_from,
        "date_to": date_to,
    }
    rows = await repo.list_events(**filters, limit=limit, offset=offset)
    total = await repo.count_events(**filters)
    return PaginatedEvents(
        rows=[EventOut.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/review", response_model=list[EventOut])
async def list_review_catalysts(
    current_user: CurrentUser,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[EventOut]:
    rows = await EventRepository(session).list_by_type_status("catalyst", "review", limit)
    return [EventOut.model_validate(r) for r in rows]


@router.get("/ignored", response_model=PaginatedEvents)
async def list_ignored_catalysts(
    current_user: CurrentUser,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=1000)] = 500,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PaginatedEvents:
    """Catalysts dropped before the review queue: prefilter noise or LLM
    judged not company-critical. Used to audit for false negatives.
    Paginated because this list grows unbounded (no cleanup) and the UI
    needs the total to indicate whether more rows exist past the page."""
    repo = EventRepository(session)
    rows = await repo.list_by_type_status("catalyst", "ignored", limit=limit, offset=offset)
    total = await repo.count_by_type_status("catalyst", "ignored")
    return PaginatedEvents(
        rows=[EventOut.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


class IpoExchangeRow(BaseModel):
    source: str | None
    exchange: str | None
    country: str | None
    count: int
    alerting: bool


class IpoCoverageOut(BaseModel):
    enabled_countries: list[str]
    exchange_country_map: dict[str, str]
    exchanges: list[IpoExchangeRow]


@router.get("/ipo/coverage", response_model=IpoCoverageOut)
async def ipo_coverage(
    current_user: CurrentUser,
    session: SessionDep,
) -> IpoCoverageOut:
    """Exchange→country mapping, enabled countries, and how many fetched
    IPO events fall under each exchange — i.e. what is/isn't being alerted."""
    from catalyst_radar.runtime_config import effective
    from catalyst_radar.services.ipo_sync import (
        _EXCHANGE_COUNTRY,
        _enabled_countries,
        country_for_exchange,
    )

    eff = await effective(session)
    enabled = _enabled_countries(eff.eodhd_ipo_enabled_countries)
    counts = await EventRepository(session).ipo_exchange_counts()
    rows = [
        IpoExchangeRow(
            source=src,
            exchange=ex,
            country=country_for_exchange(ex),
            count=n,
            alerting=bool(country_for_exchange(ex) and country_for_exchange(ex) in enabled),
        )
        for src, ex, n in counts
    ]
    return IpoCoverageOut(
        enabled_countries=sorted(enabled),
        exchange_country_map=dict(sorted(_EXCHANGE_COUNTRY.items())),
        exchanges=rows,
    )


@router.post("/{event_id}/feedback", response_model=EventOut)
async def submit_feedback(
    event_id: int,
    payload: FeedbackRequest,
    current_user: CurrentUser,
    session: SessionDep,
) -> EventOut:
    repo = EventRepository(session)
    event = await repo.get(event_id)
    if event is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="event not found")
    event = await repo.set_feedback(event, payload.label)
    return EventOut.model_validate(event)


class DecideRequest(BaseModel):
    action: DecisionAction


class DecideResponse(BaseModel):
    event: EventOut
    notification_id: int | None = None


async def _decide_one(
    session: SessionDep,
    event_id: int,
    action: DecisionAction,
) -> DecideResponse:
    """Atomic state transition for one event.

    Why the actions branch this way: the catalyst classifier already
    queues a Notification (status='review') for every event that reaches
    the review queue; ignored events have no notification row. So 'send'
    only needs to flip an existing notification, but 'promote' has to
    create one from scratch."""
    erepo = EventRepository(session)
    event = await erepo.get(event_id)
    if event is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="event not found")

    nrepo = NotificationRepository(session)
    existing = await nrepo.get_by_event(event_id)
    now_iso = utcnow().isoformat()

    if action == "send":
        if event.status != "review":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"send requires status=review, got '{event.status}'",
            )
        # Flip queued notification → pending so dispatcher delivers it.
        # dispatch_after gates pickup for _SEND_DISPATCH_GRACE seconds so
        # the undo toast can recall the row before Celery beat fires.
        if existing is not None and existing.status in ("review", "skipped", "failed"):
            existing.status = "pending"
            existing.skip_reason = None
            existing.dispatch_after = utcnow() + _SEND_DISPATCH_GRACE
            await nrepo.save(existing)
        event = await erepo.update_status_and_payload(
            event,
            status="notified",
            payload_merge={"decided_from": "review", "decided_at": now_iso},
        )
        return DecideResponse(
            event=EventOut.model_validate(event),
            notification_id=existing.id if existing else None,
        )

    if action == "ignore":
        if event.status != "review":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"ignore requires status=review, got '{event.status}'",
            )
        if existing is not None and existing.status == "review":
            existing.status = "skipped"
            existing.skip_reason = "user_ignored"
            await nrepo.save(existing)
        event = await erepo.update_status_and_payload(
            event,
            status="ignored",
            payload_merge={
                "feedback": "not_useful",
                "decided_from": "review",
                "decided_at": now_iso,
            },
        )
        return DecideResponse(
            event=EventOut.model_validate(event),
            notification_id=existing.id if existing else None,
        )

    if action == "promote":
        if event.status != "ignored":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"promote requires status=ignored, got '{event.status}'",
            )
        # Realign event.dedup_key from the sid-based key (_ignored_event)
        # to the classifier's content-key form. Without this, a future
        # classifier run on the same article via a different source has
        # a different keyspace from the promoted event and would create a
        # parallel notification. After realign, that classifier run hits
        # events_repo.upsert's get_by_dedup_key branch and dedups into
        # this event, never creating a second alert.
        from catalyst_radar.dedup import (
            content_dedup_key,
            content_text_key,
        )
        from catalyst_radar.dedup import (
            dedup_key as make_dedup_key,
        )
        from catalyst_radar.services.alerts import format_catalyst

        news = (event.payload or {}).get("news") or {}
        link = news.get("link")
        title = news.get("title") or event.title
        new_key: str | None = None
        if link:
            new_key = content_dedup_key(event.symbol, event.exchange, link)
        elif title:
            new_key = content_text_key(event.symbol, event.exchange, title)
        if new_key and new_key != event.dedup_key:
            prior = await erepo.get_by_dedup_key(new_key)
            if prior is not None and prior.id != event.id:
                # A classifier event already covers this article — promoting
                # would create a duplicate alert. Surface the conflict so the
                # user can act on the canonical row instead.
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"event {prior.id} already covers this article "
                        f"(status={prior.status}); cannot promote duplicate"
                    ),
                )
            event.dedup_key = new_key
            erepo.session.add(event)
            await erepo.session.flush()

        text = format_catalyst(event)
        dkey = make_dedup_key(event.dedup_key, "catalyst")
        # Standard catalyst dedup_key now (no ':promoted' suffix). The
        # by-event lookup at the top of _decide_one usually finds nothing
        # for prefilter events; fall back to by-key in case a sibling
        # classifier run raced and already inserted one.
        if existing is None:
            existing = await nrepo.get_by_dedup_key(dkey)
        dispatch_at = utcnow() + _SEND_DISPATCH_GRACE
        if existing is None:
            n = Notification(
                event_id=event.id,
                channel="telegram",
                dedup_key=dkey,
                status="pending",
                payload={"text": text},
                dispatch_after=dispatch_at,
            )
            n = await nrepo.add(n)
            nid = n.id
        else:
            existing.status = "pending"
            existing.skip_reason = None
            existing.payload = {"text": text}
            existing.dispatch_after = dispatch_at
            await nrepo.save(existing)
            nid = existing.id
        event = await erepo.update_status_and_payload(
            event,
            status="notified",
            payload_merge={"decided_from": "ignored", "decided_at": now_iso},
        )
        return DecideResponse(event=EventOut.model_validate(event), notification_id=nid)

    if action == "restore_to_review":
        # Undo for Send/Ignore from the review tab. If the dispatcher
        # already sent the message, the user just has to live with it —
        # this only resets bookkeeping, never recalls a sent Telegram.
        # Clearing dispatch_after isn't strictly necessary (review-status
        # rows aren't selected by the dispatcher) but keeps the column
        # honest: it always reflects a live grace window.
        if existing is not None and existing.status in ("pending", "skipped"):
            existing.status = "review"
            existing.skip_reason = None
            existing.dispatch_after = None
            await nrepo.save(existing)
        event = await erepo.update_status_and_payload(
            event,
            status="review",
            payload_unset=["decided_from", "decided_at", "feedback"],
        )
        return DecideResponse(
            event=EventOut.model_validate(event),
            notification_id=existing.id if existing else None,
        )

    if action == "restore_to_ignored":
        # Undo for Promote. The notification we created (or revived) goes
        # back to skipped so the dispatcher doesn't fire it. Clear
        # dispatch_after for the same reason as restore_to_review.
        if existing is not None and existing.status == "pending":
            existing.status = "skipped"
            existing.skip_reason = "user_undid_promote"
            existing.dispatch_after = None
            await nrepo.save(existing)
        event = await erepo.update_status_and_payload(
            event,
            status="ignored",
            payload_unset=["decided_from", "decided_at"],
        )
        return DecideResponse(
            event=EventOut.model_validate(event),
            notification_id=existing.id if existing else None,
        )

    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=f"unknown action '{action}'",
    )


@router.post("/{event_id}/decide", response_model=DecideResponse)
async def decide_event(
    event_id: int,
    payload: DecideRequest,
    current_user: CurrentUser,
    session: SessionDep,
) -> DecideResponse:
    return await _decide_one(session, event_id, payload.action)


class BulkDecideRequest(BaseModel):
    ids: list[int]
    action: DecisionAction


class BulkDecideFailure(BaseModel):
    id: int
    reason: str


class BulkDecideResponse(BaseModel):
    ok: list[int]
    failed: list[BulkDecideFailure]


@router.post("/decide", response_model=BulkDecideResponse)
async def bulk_decide(
    payload: BulkDecideRequest,
    current_user: CurrentUser,
    session: SessionDep,
) -> BulkDecideResponse:
    """Apply the same action to many events at once. One ID's failure
    does not abort the rest — the UI's bulk bar needs partial success
    so the user can see exactly which rows fell out."""
    ok: list[int] = []
    failed: list[BulkDecideFailure] = []
    for eid in payload.ids:
        try:
            await _decide_one(session, eid, payload.action)
            ok.append(eid)
        except HTTPException as exc:
            failed.append(BulkDecideFailure(id=eid, reason=str(exc.detail)))
    return BulkDecideResponse(ok=ok, failed=failed)
