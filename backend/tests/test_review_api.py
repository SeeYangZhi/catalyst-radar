from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from catalyst_radar.models.company import TrackedCompany
from catalyst_radar.models.event import Event
from catalyst_radar.models.notification import Notification
from catalyst_radar.services.catalyst_sync import sync_catalysts
from tests.test_catalyst_sync import _FakeClassifier, _NewsStub


async def _auth(client: AsyncClient, creds: dict[str, str]) -> dict[str, str]:
    token = (await client.post("/api/v1/auth/token", data=creds)).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


async def _seed(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as s:
        s.add(
            TrackedCompany(
                symbol="AAPL",
                exchange="US",
                company_name="Apple Inc",
                source="manual",
            )
        )
        await s.commit()
        await sync_catalysts(s, news_adapter=_NewsStub(), classifier=_FakeClassifier())


async def test_review_queue_approve_and_feedback(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    admin_credentials: dict[str, str],
) -> None:
    await _seed(session_factory)
    h = await _auth(client, admin_credentials)

    review = await client.get("/api/v1/events/review", headers=h)
    assert review.status_code == 200
    items = review.json()
    assert len(items) == 1  # the medium/0.7 partnership catalyst
    event_id = items[0]["id"]

    fb = await client.post(
        f"/api/v1/events/{event_id}/feedback",
        headers=h,
        json={"label": "useful"},
    )
    assert fb.status_code == 200
    assert fb.json()["payload"]["feedback"] == "useful"

    bad = await client.post(
        f"/api/v1/events/{event_id}/feedback",
        headers=h,
        json={"label": "bogus"},
    )
    assert bad.status_code == 422

    queued = await client.get("/api/v1/notifications", headers=h, params={"status": "review"})
    nid = queued.json()[0]["id"]
    approved = await client.post(f"/api/v1/notifications/{nid}/approve", headers=h)
    assert approved.status_code == 200
    assert approved.json()["status"] == "pending"


async def test_decide_send_transitions_event_and_notification(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    admin_credentials: dict[str, str],
) -> None:
    """Send from review tab: event status review→notified, queued
    notification flipped review→pending, decided_from='review' recorded."""
    await _seed(session_factory)
    h = await _auth(client, admin_credentials)
    items = (await client.get("/api/v1/events/review", headers=h)).json()
    eid = items[0]["id"]

    r = await client.post(f"/api/v1/events/{eid}/decide", headers=h, json={"action": "send"})
    assert r.status_code == 200
    body = r.json()
    assert body["event"]["status"] == "notified"
    assert body["event"]["payload"]["decided_from"] == "review"
    assert "decided_at" in body["event"]["payload"]
    assert body["notification_id"] is not None

    async with session_factory() as s:
        n = await s.get(Notification, body["notification_id"])
        assert n.status == "pending"
        assert n.skip_reason is None

    # Send again on a now-notified row is a state-machine error, not idempotent.
    again = await client.post(f"/api/v1/events/{eid}/decide", headers=h, json={"action": "send"})
    assert again.status_code == 409


async def test_decide_ignore_skips_notification_and_records_feedback(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    admin_credentials: dict[str, str],
) -> None:
    """Ignore from review tab: status→ignored, notification skipped with
    reason, payload.feedback='not_useful' (the only feedback label we still
    care about — the explicit positive signal is just status=notified)."""
    await _seed(session_factory)
    h = await _auth(client, admin_credentials)
    eid = (await client.get("/api/v1/events/review", headers=h)).json()[0]["id"]

    r = await client.post(f"/api/v1/events/{eid}/decide", headers=h, json={"action": "ignore"})
    assert r.status_code == 200
    body = r.json()
    assert body["event"]["status"] == "ignored"
    assert body["event"]["payload"]["feedback"] == "not_useful"
    assert body["event"]["payload"]["decided_from"] == "review"

    async with session_factory() as s:
        n = await s.get(Notification, body["notification_id"])
        assert n.status == "skipped"
        assert n.skip_reason == "user_ignored"


async def test_decide_promote_creates_notification_for_ignored_event(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    admin_credentials: dict[str, str],
) -> None:
    """Promote from ignored tab: prefilter-dropped events have no
    notification row, so promote must CREATE one (status=pending) and
    flip the event to notified."""
    await _seed(session_factory)
    h = await _auth(client, admin_credentials)
    ignored = (await client.get("/api/v1/events/ignored", headers=h)).json()["rows"]
    assert len(ignored) >= 1
    eid = ignored[0]["id"]

    # Pre-condition: no notification exists for this ignored event.
    async with session_factory() as s:
        existing = (
            await s.execute(select(Notification).where(Notification.event_id == eid))
        ).scalar_one_or_none()
        assert existing is None

    r = await client.post(f"/api/v1/events/{eid}/decide", headers=h, json={"action": "promote"})
    assert r.status_code == 200
    body = r.json()
    assert body["event"]["status"] == "notified"
    assert body["event"]["payload"]["decided_from"] == "ignored"
    # Original ignore_reason from the prefilter is preserved so a later
    # prompt-tuning review can see *why* the prefilter dropped it.
    assert "ignore_reason" in body["event"]["payload"]

    async with session_factory() as s:
        n = await s.get(Notification, body["notification_id"])
        assert n.status == "pending"
        assert n.event_id == eid


async def test_decide_promote_realigns_dedup_key_so_classifier_dedups(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    admin_credentials: dict[str, str],
) -> None:
    """Promote rewrites event.dedup_key from the sid-based form (used by
    _ignored_event) to the classifier's content_dedup_key form. Without
    this, a later classifier run on the same article via a different
    source has a different keyspace and creates a parallel notification.
    Asserted directly here by comparing keys; the upsert-dedup behaviour
    is the load-bearing guarantee on the classifier side."""
    from catalyst_radar.dedup import content_dedup_key

    await _seed(session_factory)
    h = await _auth(client, admin_credentials)
    ignored = (await client.get("/api/v1/events/ignored", headers=h)).json()["rows"]
    target = next(
        ev for ev in ignored if (ev["payload"].get("news") or {}).get("link")
    )
    eid = target["id"]
    link = target["payload"]["news"]["link"]
    expected_key = content_dedup_key(target["symbol"], target["exchange"], link)

    async with session_factory() as s:
        before = await s.get(Event, eid)
        assert before.dedup_key != expected_key, "fixture invariant: sid-keyed before promote"

    r = await client.post(f"/api/v1/events/{eid}/decide", headers=h, json={"action": "promote"})
    assert r.status_code == 200

    async with session_factory() as s:
        after = await s.get(Event, eid)
        assert after.dedup_key == expected_key, "promote must realign to content_dedup_key"


async def test_decide_promote_conflicts_when_classifier_event_already_covers_article(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    admin_credentials: dict[str, str],
) -> None:
    """If a classifier-path event already exists at the content_dedup_key,
    promote refuses with 409 — would otherwise double-alert."""
    from catalyst_radar.dedup import content_dedup_key

    await _seed(session_factory)
    h = await _auth(client, admin_credentials)
    ignored = (await client.get("/api/v1/events/ignored", headers=h)).json()["rows"]
    target = next(
        ev for ev in ignored if (ev["payload"].get("news") or {}).get("link")
    )
    eid = target["id"]
    link = target["payload"]["news"]["link"]
    content_key = content_dedup_key(target["symbol"], target["exchange"], link)

    # Plant a second event at the content_key, simulating a classifier
    # run on the same article from a different source between ignore and
    # promote. This is the exact race the realign is designed to prevent.
    async with session_factory() as s:
        s.add(
            Event(
                event_type="catalyst",
                source_name="websearch_news",
                source_event_id=f"websearch:{eid}:collision",
                dedup_key=content_key,
                symbol=target["symbol"],
                exchange=target["exchange"],
                company_name=target.get("company_name"),
                title="Same story from another source",
                status="notified",
                payload={"news": {"link": link}},
            )
        )
        await s.commit()

    r = await client.post(f"/api/v1/events/{eid}/decide", headers=h, json={"action": "promote"})
    assert r.status_code == 409
    assert "already covers" in r.json()["detail"]


async def test_decide_send_sets_dispatch_after_grace(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    admin_credentials: dict[str, str],
) -> None:
    """Send stamps dispatch_after ~6s in the future so the undo toast can
    recall the row before the Celery beat picks it up."""
    from datetime import UTC

    from catalyst_radar.models.base import utcnow

    await _seed(session_factory)
    h = await _auth(client, admin_credentials)
    eid = (await client.get("/api/v1/events/review", headers=h)).json()[0]["id"]

    before = utcnow()
    r = await client.post(f"/api/v1/events/{eid}/decide", headers=h, json={"action": "send"})
    assert r.status_code == 200
    nid = r.json()["notification_id"]

    async with session_factory() as s:
        n = await s.get(Notification, nid)
        assert n.dispatch_after is not None
        # SQLite drops tz info on storage; coerce naive→UTC for comparison.
        da = n.dispatch_after if n.dispatch_after.tzinfo else n.dispatch_after.replace(tzinfo=UTC)
        # +/- some slack to avoid flakes; key claim is "in the future".
        assert da > before


async def test_decide_restore_to_review_clears_dispatch_after(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    admin_credentials: dict[str, str],
) -> None:
    """Undo must clear dispatch_after so the column stays honest (always
    reflects a live grace window). Without this, an undone-then-resent
    row would inherit a stale gate."""
    await _seed(session_factory)
    h = await _auth(client, admin_credentials)
    eid = (await client.get("/api/v1/events/review", headers=h)).json()[0]["id"]

    sent = await client.post(f"/api/v1/events/{eid}/decide", headers=h, json={"action": "send"})
    nid = sent.json()["notification_id"]

    await client.post(
        f"/api/v1/events/{eid}/decide", headers=h, json={"action": "restore_to_review"}
    )

    async with session_factory() as s:
        n = await s.get(Notification, nid)
        assert n.status == "review"
        assert n.dispatch_after is None


async def test_decide_restore_to_review_reverts_send(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    admin_credentials: dict[str, str],
) -> None:
    """Undo flow: after Send, restore_to_review must put both event and
    notification back. The undo toast in the UI fires this within ~5s."""
    await _seed(session_factory)
    h = await _auth(client, admin_credentials)
    eid = (await client.get("/api/v1/events/review", headers=h)).json()[0]["id"]

    sent = await client.post(f"/api/v1/events/{eid}/decide", headers=h, json={"action": "send"})
    nid = sent.json()["notification_id"]

    restored = await client.post(
        f"/api/v1/events/{eid}/decide", headers=h, json={"action": "restore_to_review"}
    )
    assert restored.status_code == 200
    assert restored.json()["event"]["status"] == "review"
    assert "decided_from" not in restored.json()["event"]["payload"]

    async with session_factory() as s:
        n = await s.get(Notification, nid)
        assert n.status == "review"


async def test_bulk_decide_partial_success(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    admin_credentials: dict[str, str],
) -> None:
    """Bulk endpoint underwrites the UI bulk bar; one bad id must not
    abort the rest so the user sees exactly which rows fell out."""
    await _seed(session_factory)
    h = await _auth(client, admin_credentials)
    review = (await client.get("/api/v1/events/review", headers=h)).json()
    good = review[0]["id"]

    r = await client.post(
        "/api/v1/events/decide",
        headers=h,
        json={"ids": [good, 999_999], "action": "ignore"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] == [good]
    assert len(body["failed"]) == 1
    assert body["failed"][0]["id"] == 999_999

    async with session_factory() as s:
        ev = await s.get(Event, good)
        assert ev.status == "ignored"


async def test_ops_endpoints(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    admin_credentials: dict[str, str],
) -> None:
    await _seed(session_factory)
    h = await _auth(client, admin_credentials)

    assert (await client.get("/api/v1/source-runs")).status_code == 401

    runs = await client.get("/api/v1/source-runs", headers=h)
    assert runs.status_code == 200
    assert len(runs.json()) >= 1

    clf = await client.get("/api/v1/classifier-runs", headers=h)
    assert clf.status_code == 200
    assert len(clf.json()) == 3  # one per LLM call in the fixture run

    summary = await client.get("/api/v1/dashboard/summary", headers=h)
    body = summary.json()
    assert body["tracked_companies"] == 1
    assert body["catalyst_events"] >= 2
    assert body["catalysts_in_review"] == 1
    assert body["last_source_run"]["source_name"].startswith("eodhd_news")
