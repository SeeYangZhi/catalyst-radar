"""Feedback API: POST /api/v1/events/{event_id}/feedback.

Phase 8 contract — all four labels (useful / not_useful / false_positive /
false_negative) are accepted and persisted into payload["feedback"];
unknown labels are rejected with 422; auth is required.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from catalyst_radar.models.event import Event

FEEDBACK_LABELS = ("useful", "not_useful", "false_positive", "false_negative")


async def _auth(client: AsyncClient, creds: dict[str, str]) -> dict[str, str]:
    token = (await client.post("/api/v1/auth/token", data=creds)).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


async def _seed_event(factory: async_sessionmaker[AsyncSession]) -> int:
    async with factory() as s:
        event = Event(
            event_type="catalyst",
            source_name="test",
            source_event_id="test:feedback:1",
            dedup_key="test:feedback:1",
            symbol="AAPL.US",
            company_name="Apple Inc",
            title="Test catalyst headline",
            status="review",
            payload={},
        )
        s.add(event)
        await s.commit()
        await s.refresh(event)
        assert event.id is not None
        return event.id


@pytest.mark.parametrize("label", FEEDBACK_LABELS)
async def test_each_label_accepted_and_persisted(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    admin_credentials: dict[str, str],
    label: str,
) -> None:
    event_id = await _seed_event(session_factory)
    h = await _auth(client, admin_credentials)

    resp = await client.post(
        f"/api/v1/events/{event_id}/feedback",
        headers=h,
        json={"label": label},
    )
    assert resp.status_code == 200
    assert resp.json()["payload"]["feedback"] == label

    # Read back through the API — feedback must survive a reload.
    review = await client.get("/api/v1/events/review", headers=h)
    assert review.status_code == 200
    rows = {r["id"]: r for r in review.json()}
    assert rows[event_id]["payload"]["feedback"] == label


async def test_relabel_overwrites_previous_feedback(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    admin_credentials: dict[str, str],
) -> None:
    event_id = await _seed_event(session_factory)
    h = await _auth(client, admin_credentials)

    first = await client.post(
        f"/api/v1/events/{event_id}/feedback",
        headers=h,
        json={"label": "useful"},
    )
    assert first.status_code == 200
    second = await client.post(
        f"/api/v1/events/{event_id}/feedback",
        headers=h,
        json={"label": "false_positive"},
    )
    assert second.status_code == 200
    assert second.json()["payload"]["feedback"] == "false_positive"


async def test_unknown_label_rejected_422(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    admin_credentials: dict[str, str],
) -> None:
    event_id = await _seed_event(session_factory)
    h = await _auth(client, admin_credentials)

    resp = await client.post(
        f"/api/v1/events/{event_id}/feedback",
        headers=h,
        json={"label": "bogus"},
    )
    assert resp.status_code == 422
    # The label was never written.
    review = await client.get("/api/v1/events/review", headers=h)
    rows = {r["id"]: r for r in review.json()}
    assert "feedback" not in (rows[event_id]["payload"] or {})


async def test_unauthenticated_rejected_401(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    event_id = await _seed_event(session_factory)
    resp = await client.post(
        f"/api/v1/events/{event_id}/feedback",
        json={"label": "useful"},
    )
    assert resp.status_code == 401


async def test_missing_event_404(
    client: AsyncClient,
    admin_credentials: dict[str, str],
) -> None:
    h = await _auth(client, admin_credentials)
    resp = await client.post(
        "/api/v1/events/999999/feedback",
        headers=h,
        json={"label": "useful"},
    )
    assert resp.status_code == 404
