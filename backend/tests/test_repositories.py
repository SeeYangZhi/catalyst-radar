"""Repository-layer characterization suite .

Pins the contracts of EventRepository (upsert idempotency, payload-merge key
preservation, set_feedback, update_status_and_payload), ConfigRepository
(set/get round-trip), and the notification repositories (TelegramChat
register, Notification status transitions + history filters) against the
in-memory SQLite ``db_session`` fixture. No network, no stubs needed —
repositories are pure DB access.
"""

from datetime import UTC, datetime

from catalyst_radar.models.event import Event
from catalyst_radar.models.notification import Notification
from catalyst_radar.repositories.config_repository import ConfigRepository
from catalyst_radar.repositories.event_repository import EventRepository
from catalyst_radar.repositories.notification_repository import (
    NotificationRepository,
    TelegramChatRepository,
)


def _event(**over) -> Event:
    base = dict(
        event_type="catalyst",
        source_name="eodhd_news",
        source_event_id="eodhd_news:test:1",
        dedup_key="url:example.com/story-1",
        symbol="0001.HK",
        exchange="HK",
        title="Test story",
        payload={"summary": "initial"},
    )
    base.update(over)
    return Event(**base)


# ---------------------------------------------------------------------------
# EventRepository.upsert — idempotency
# ---------------------------------------------------------------------------


async def test_upsert_same_source_event_id_twice_yields_one_row(db_session):
    repo = EventRepository(db_session)

    first, created_first = await repo.upsert(_event())
    second, created_second = await repo.upsert(_event())

    assert created_first is True
    assert created_second is False
    assert second.id == first.id
    assert await repo.count_by_type("catalyst") == 1


async def test_upsert_refreshes_title_and_url_but_keeps_existing_when_blank(db_session):
    repo = EventRepository(db_session)
    await repo.upsert(_event(title="Old title", source_url="https://a.example/1"))

    # Non-empty incoming values win ...
    updated, created = await repo.upsert(
        _event(title="New title", source_url="https://a.example/2")
    )
    assert created is False
    assert updated.title == "New title"
    assert updated.source_url == "https://a.example/2"

    # ... but empty incoming values never clobber what's stored.
    kept, _ = await repo.upsert(_event(title=None, source_url=None))
    assert kept.title == "New title"
    assert kept.source_url == "https://a.example/2"


async def test_upsert_same_dedup_key_different_source_appends_also_seen_in(db_session):
    """Cross-source URL dedup: second source for the same story does not
    create a second row — it is recorded under payload.also_seen_in."""
    repo = EventRepository(db_session)
    original, _ = await repo.upsert(_event())

    other_source = _event(
        source_name="eastmoney_news",
        source_event_id="eastmoney_news:test:99",
        source_url="https://example.com/story-1",
    )
    dupe, created = await repo.upsert(other_source)

    assert created is False
    assert dupe.id == original.id
    seen = dupe.payload["also_seen_in"]
    assert len(seen) == 1
    assert seen[0]["source"] == "eastmoney_news"
    assert seen[0]["source_event_id"] == "eastmoney_news:test:99"
    assert seen[0]["via"] == "url"


# ---------------------------------------------------------------------------
# EventRepository.upsert — payload merge-by-default on source refresh
#
# Contract: incoming source keys overwrite (including explicit None values —
# adapters emit their full key set every run), keys absent from the incoming
# payload survive. Replaces the old hardcoded preserved-key allowlist that
# needed bug-driven additions (also_seen_in, listing_confirmed) and still
# missed keys (decided_from/decided_at, financials).
# ---------------------------------------------------------------------------


async def test_upsert_preserves_feedback_across_payload_refresh(db_session):
    repo = EventRepository(db_session)
    existing, _ = await repo.upsert(_event())

    payload = dict(existing.payload or {})
    payload["feedback"] = "useful"
    existing.payload = payload
    db_session.add(existing)
    await db_session.flush()

    refreshed, created = await repo.upsert(_event(payload={"summary": "re-synced"}))

    assert created is False
    assert refreshed.payload["feedback"] == "useful"
    assert refreshed.payload["summary"] == "re-synced"


async def test_upsert_preserves_profile_across_payload_refresh(db_session):
    repo = EventRepository(db_session)
    existing, _ = await repo.upsert(_event())

    payload = dict(existing.payload or {})
    payload["profile"] = {"description": "Maker of widgets"}
    existing.payload = payload
    db_session.add(existing)
    await db_session.flush()

    refreshed, _ = await repo.upsert(_event(payload={"summary": "re-synced"}))

    assert refreshed.payload["profile"] == {"description": "Maker of widgets"}


async def test_upsert_preserves_also_seen_in_across_payload_refresh(db_session):
    repo = EventRepository(db_session)
    existing, _ = await repo.upsert(_event())
    await repo.append_also_seen_in(
        existing, {"source": "eastmoney_news", "url": "https://b.example/1"}
    )

    refreshed, _ = await repo.upsert(_event(payload={"summary": "re-synced"}))

    assert refreshed.payload["also_seen_in"] == [
        {"source": "eastmoney_news", "url": "https://b.example/1"}
    ]


async def test_upsert_merge_by_default_unknown_existing_key_survives_refresh(db_session):
    """ANY payload key written outside source sync survives a refresh — not
    just the historically allowlisted ones. Pins the merge-by-default policy
    so the next enrichment-owned key cannot silently vanish on resync."""
    repo = EventRepository(db_session)
    existing, _ = await repo.upsert(_event())

    payload = dict(existing.payload or {})
    payload["decided_from"] = "review"  # /decide endpoint write
    payload["some_future_enrichment_key"] = {"checked": True}
    existing.payload = payload
    db_session.add(existing)
    await db_session.flush()

    refreshed, created = await repo.upsert(_event(payload={"summary": "re-synced"}))

    assert created is False
    assert refreshed.payload["decided_from"] == "review"
    assert refreshed.payload["some_future_enrichment_key"] == {"checked": True}
    assert refreshed.payload["summary"] == "re-synced"


async def test_upsert_incoming_payload_keys_still_overwrite_on_refresh(db_session):
    """Merge-by-default does not weaken source ownership: every key present
    in the incoming payload wins, including explicit None values (adapters
    emit their full key set, so a present-but-None key means the source now
    says 'empty')."""
    repo = EventRepository(db_session)
    await repo.upsert(_event(payload={"summary": "initial", "offer_price": 10.0}))

    refreshed, _ = await repo.upsert(
        _event(payload={"summary": "updated", "offer_price": None})
    )

    assert refreshed.payload["summary"] == "updated"
    assert refreshed.payload["offer_price"] is None


async def test_upsert_with_no_incoming_payload_leaves_existing_payload_alone(db_session):
    repo = EventRepository(db_session)
    await repo.upsert(_event(payload={"summary": "initial", "feedback": "useful"}))

    refreshed, created = await repo.upsert(_event(payload=None))

    assert created is False
    assert refreshed.payload == {"summary": "initial", "feedback": "useful"}


# ---------------------------------------------------------------------------
# EventRepository.set_feedback
# ---------------------------------------------------------------------------


async def test_set_feedback_persists_label_and_keeps_other_payload_keys(db_session):
    repo = EventRepository(db_session)
    event, _ = await repo.upsert(_event(payload={"summary": "initial"}))

    updated = await repo.set_feedback(event, "not_useful")

    assert updated.payload["feedback"] == "not_useful"
    assert updated.payload["summary"] == "initial"

    # Re-read from the DB: the write was committed, not just flushed.
    fetched = await repo.get(event.id)
    assert fetched.payload["feedback"] == "not_useful"


async def test_set_feedback_overwrites_previous_label(db_session):
    repo = EventRepository(db_session)
    event, _ = await repo.upsert(_event())

    await repo.set_feedback(event, "useful")
    updated = await repo.set_feedback(event, "not_useful")

    assert updated.payload["feedback"] == "not_useful"


async def test_set_feedback_on_event_with_null_payload(db_session):
    repo = EventRepository(db_session)
    event, _ = await repo.upsert(_event(payload=None))

    updated = await repo.set_feedback(event, "useful")

    assert updated.payload == {"feedback": "useful"}


# ---------------------------------------------------------------------------
# EventRepository.update_status_and_payload
# ---------------------------------------------------------------------------


async def test_update_status_and_payload_sets_status_and_merges(db_session):
    repo = EventRepository(db_session)
    event, _ = await repo.upsert(_event(payload={"summary": "initial"}))

    updated = await repo.update_status_and_payload(
        event,
        status="notified",
        payload_merge={
            "decided_from": "web",
            "decided_at": datetime(2026, 6, 4, 12, 0, tzinfo=UTC).isoformat(),
        },
    )

    assert updated.status == "notified"
    assert updated.payload["decided_from"] == "web"
    assert updated.payload["summary"] == "initial"  # merge, not replace

    fetched = await repo.get(event.id)
    assert fetched.status == "notified"
    assert fetched.payload["decided_from"] == "web"


async def test_update_status_and_payload_unset_removes_keys(db_session):
    """events.py:358 undo path: decided_from/decided_at/feedback must be
    GONE from the payload, not set to null."""
    repo = EventRepository(db_session)
    event, _ = await repo.upsert(
        _event(
            payload={
                "summary": "initial",
                "decided_from": "web",
                "decided_at": "2026-06-04T12:00:00+00:00",
                "feedback": "useful",
            }
        )
    )

    updated = await repo.update_status_and_payload(
        event,
        status="new",
        payload_unset=["decided_from", "decided_at", "feedback"],
    )

    assert updated.status == "new"
    assert "decided_from" not in updated.payload
    assert "decided_at" not in updated.payload
    assert "feedback" not in updated.payload
    assert updated.payload["summary"] == "initial"


async def test_update_status_and_payload_unset_missing_key_is_noop(db_session):
    repo = EventRepository(db_session)
    event, _ = await repo.upsert(_event(payload={"summary": "initial"}))

    updated = await repo.update_status_and_payload(
        event, status="ignored", payload_unset=["never_existed"]
    )

    assert updated.status == "ignored"
    assert updated.payload == {"summary": "initial"}


async def test_update_status_and_payload_merge_and_unset_together(db_session):
    repo = EventRepository(db_session)
    event, _ = await repo.upsert(_event(payload={"feedback": "useful"}))

    updated = await repo.update_status_and_payload(
        event,
        status="relevant",
        payload_merge={"decided_from": "telegram"},
        payload_unset=["feedback"],
    )

    assert updated.status == "relevant"
    assert updated.payload["decided_from"] == "telegram"
    assert "feedback" not in updated.payload


# ---------------------------------------------------------------------------
# ConfigRepository
# ---------------------------------------------------------------------------


async def test_config_set_get_round_trip(db_session):
    repo = ConfigRepository(db_session)

    await repo.set("alerts_enabled", True)
    assert await repo.get("alerts_enabled") is True

    # JSON values round-trip with structure intact.
    await repo.set("windows", {"days": [7, 1], "tz": "Asia/Singapore"})
    assert await repo.get("windows") == {"days": [7, 1], "tz": "Asia/Singapore"}


async def test_config_set_overwrites_existing_key(db_session):
    repo = ConfigRepository(db_session)

    await repo.set("sync_interval_minutes", 30)
    await repo.set("sync_interval_minutes", 5)

    assert await repo.get("sync_interval_minutes") == 5
    # Overwrite updates the row in place — no duplicate keys.
    assert list((await repo.all()).keys()).count("sync_interval_minutes") == 1


async def test_config_get_missing_key_returns_none(db_session):
    repo = ConfigRepository(db_session)
    assert await repo.get("never_set") is None


async def test_config_all_returns_every_row(db_session):
    repo = ConfigRepository(db_session)
    await repo.set("a", 1)
    await repo.set("b", "two")

    assert await repo.all() == {"a": 1, "b": "two"}


# ---------------------------------------------------------------------------
# TelegramChatRepository.register
# ---------------------------------------------------------------------------


async def test_register_creates_active_chat(db_session):
    repo = TelegramChatRepository(db_session)

    chat = await repo.register("12345", chat_type="private", title="Yang")

    assert chat.id is not None
    assert chat.chat_id == "12345"
    assert chat.is_active is True
    assert [c.chat_id for c in await repo.active()] == ["12345"]


async def test_register_existing_chat_reactivates_without_duplicate(db_session):
    repo = TelegramChatRepository(db_session)
    chat = await repo.register("12345", chat_type="private", title="Yang")
    chat.is_active = False
    db_session.add(chat)
    await db_session.commit()
    assert await repo.active() == []

    again = await repo.register("12345")

    assert again.id == chat.id
    assert again.is_active is True
    # Blank re-register keeps the previously stored metadata.
    assert again.chat_type == "private"
    assert again.title == "Yang"
    assert len(await repo.active()) == 1


# ---------------------------------------------------------------------------
# NotificationRepository — status transitions + history
# ---------------------------------------------------------------------------


def _notification(**over) -> Notification:
    base = dict(
        channel="telegram",
        chat_id="12345",
        dedup_key="notif:test:1",
        status="pending",
        payload={"text": "hello"},
    )
    base.update(over)
    return Notification(**base)


async def test_notification_add_then_status_transition_to_sent(db_session):
    repo = NotificationRepository(db_session)
    notif = await repo.add(_notification())
    assert notif.status == "pending"
    assert notif.attempts == 0

    notif.status = "sent"
    notif.attempts = 1
    notif.telegram_message_id = 777
    notif.sent_at = datetime(2026, 6, 4, 12, 0, tzinfo=UTC)
    saved = await repo.save(notif)

    fetched = await repo.get(saved.id)
    assert fetched.status == "sent"
    assert fetched.attempts == 1
    assert fetched.telegram_message_id == 777
    assert fetched.sent_at is not None


async def test_notification_failed_transition_records_error(db_session):
    repo = NotificationRepository(db_session)
    notif = await repo.add(_notification())

    notif.status = "failed"
    notif.attempts = 1
    notif.error = "telegram: 429 Too Many Requests"
    await repo.save(notif)

    fetched = await repo.get(notif.id)
    assert fetched.status == "failed"
    assert fetched.error == "telegram: 429 Too Many Requests"


async def test_notification_dedup_key_lookup(db_session):
    repo = NotificationRepository(db_session)
    await repo.add(_notification(dedup_key="notif:test:dedup"))

    assert (await repo.get_by_dedup_key("notif:test:dedup")) is not None
    assert (await repo.get_by_dedup_key("notif:test:other")) is None


async def test_notification_list_recent_filters_by_status(db_session):
    repo = NotificationRepository(db_session)
    await repo.add(_notification(dedup_key="n:1", status="sent"))
    await repo.add(_notification(dedup_key="n:2", status="failed"))
    await repo.add(_notification(dedup_key="n:3", status="sent"))

    sent = await repo.list_recent(status="sent")
    assert {n.dedup_key for n in sent} == {"n:1", "n:3"}
    assert all(n.status == "sent" for n in sent)

    everything = await repo.list_recent()
    assert len(everything) == 3

    assert await repo.list_recent(status="skipped") == []


async def test_notification_list_recent_respects_limit(db_session):
    repo = NotificationRepository(db_session)
    for i in range(5):
        await repo.add(_notification(dedup_key=f"n:{i}"))

    assert len(await repo.list_recent(limit=2)) == 2


async def test_notification_get_by_event_returns_latest_for_event(db_session):
    erepo = EventRepository(db_session)
    nrepo = NotificationRepository(db_session)
    event, _ = await erepo.upsert(_event())

    await nrepo.add(_notification(dedup_key="n:ev:1", event_id=event.id))

    found = await nrepo.get_by_event(event.id)
    assert found is not None
    assert found.dedup_key == "n:ev:1"
    assert await nrepo.get_by_event(event.id + 999) is None
