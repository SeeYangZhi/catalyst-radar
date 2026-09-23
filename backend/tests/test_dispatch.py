"""Delivery suite for services/dispatch.py + services/notification_service.py.

Characterization tests over the dispatch orchestration: status transitions,
Telegram message_id persistence, retry of failed sends, idempotency per
event + reminder window, and the runtime kill-switch. The Telegram client
is always a local recorder stub — no network.

Contract notes (grounded in the real code):
- ``deliver_pending_notifications`` fans one notification out to EVERY
  active chat, tracks per-chat delivery in ``payload["delivered_chats"]``,
  and persists the PRIMARY chat's ``telegram_message_id``/``chat_id`` (first
  successful delivery) so a late-landing IPO description can self-heal the
  alert in place via ``alert_backfill``. Single-user product → one active
  chat, so the singular columns fully cover the real fan-out.
- ``TelegramClient.send_message`` normally returns ``SendResult(ok=False)``
  instead of raising; a raising client is a contract violation that the
  dispatcher must still survive (poison-pill guard).
"""

from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.config import settings
from catalyst_radar.models.base import utcnow
from catalyst_radar.models.event import Event
from catalyst_radar.models.notification import Notification, TelegramChat
from catalyst_radar.services.alert_backfill import backfill_alert_edits
from catalyst_radar.services.dispatch import deliver_pending_notifications
from catalyst_radar.services.notification_service import NotificationService
from catalyst_radar.services.telegram_client import SendResult


class RecorderClient:
    """Telegram stub: records every send; can fail or raise per chat/text."""

    configured = True

    def __init__(
        self,
        *,
        ok: bool = True,
        message_id: int = 31337,
        raise_for_chats: set[str] | None = None,
        raise_for_texts: set[str] | None = None,
    ) -> None:
        self.ok = ok
        self.message_id = message_id
        self.raise_for_chats = raise_for_chats or set()
        self.raise_for_texts = raise_for_texts or set()
        self.calls: list[tuple[str, str]] = []
        self.edits: list[dict] = []

    async def send_message(
        self,
        chat_id: str,
        text: str,
        *,
        reply_markup: dict | None = None,
        max_attempts: int = 3,
    ) -> SendResult:
        self.calls.append((chat_id, text))
        if chat_id in self.raise_for_chats or text in self.raise_for_texts:
            raise RuntimeError("telegram client exploded")
        if not self.ok:
            return SendResult(ok=False, error_code=403, description="blocked")
        return SendResult(ok=True, message_id=self.message_id)

    async def edit_message_text(
        self,
        chat_id: str,
        message_id: int,
        text: str,
        *,
        reply_markup: dict | None = None,
    ) -> SendResult:
        self.edits.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "reply_markup": reply_markup,
            }
        )
        return SendResult(ok=True, message_id=message_id)


def _notification(dedup_key: str, text: str, **over) -> Notification:
    base: dict = dict(
        channel="telegram",
        dedup_key=dedup_key,
        status="pending",
        payload={"text": text},
    )
    base.update(over)
    return Notification(**base)


async def _count(session: AsyncSession, dedup_key: str) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(Notification)
            .where(Notification.dedup_key == dedup_key)
        )
    ).scalar_one()


# ---------------------------------------------------------------------------
# deliver_pending_notifications — happy path + idempotent re-run
# ---------------------------------------------------------------------------


async def test_successful_send_marks_sent_and_persists_delivery_state(
    db_session: AsyncSession,
) -> None:
    db_session.add(TelegramChat(chat_id="111", is_active=True))
    db_session.add(_notification("evt:ok:7", "earnings alert"))
    await db_session.commit()

    client = RecorderClient()
    out = await deliver_pending_notifications(db_session, client=client)
    assert (out.sent, out.failed, out.skipped) == (1, 0, 0)
    assert client.calls == [("111", "earnings alert")]

    row = (
        await db_session.execute(
            select(Notification).where(Notification.dedup_key == "evt:ok:7")
        )
    ).scalar_one()
    assert row.status == "sent"
    assert row.sent_at is not None
    assert row.error is None
    assert row.attempts == 1
    assert row.payload["delivered_chats"] == ["111"]

    # Re-run is a no-op: the sent row is not picked up again, nothing re-sent.
    again = await deliver_pending_notifications(db_session, client=client)
    assert (again.sent, again.failed, again.skipped) == (0, 0, 0)
    assert len(client.calls) == 1


async def test_successful_send_persists_message_id_for_backfill(
    db_session: AsyncSession,
) -> None:
    """The Telegram message id (and chat) of the delivered alert must be
    persisted — alert_backfill / get_sent_by_event address the message by
    telegram_message_id to self-heal a late-landing description. Without this
    the column stays NULL and the self-heal can never fire (the prod bug:
    IPO alerts that lost the inline-enrich race never grew their blurb)."""
    db_session.add(TelegramChat(chat_id="111", is_active=True))
    db_session.add(_notification("evt:mid:7", "earnings alert"))
    await db_session.commit()

    client = RecorderClient(message_id=98765)
    await deliver_pending_notifications(db_session, client=client)

    row = (
        await db_session.execute(
            select(Notification).where(Notification.dedup_key == "evt:mid:7")
        )
    ).scalar_one()
    assert row.telegram_message_id == 98765
    assert row.chat_id == "111"


async def test_dispatch_then_backfill_self_heals_late_description(
    db_session: AsyncSession,
) -> None:
    """End-to-end of the prod regression: an IPO alert ships BEFORE its
    description lands, then a later enrich run edits it in place. This closes
    the dispatch→backfill seam the unit tests missed (the backfill suite
    hand-set telegram_message_id, so it never caught that real dispatch left
    it NULL)."""
    db_session.add(TelegramChat(chat_id="111", is_active=True))
    event = Event(
        event_type="ipo",
        source_name="eodhd.ipos",
        source_event_id="ipo:LATE",
        dedup_key="dk-ipo-late",
        symbol="LATE",
        country="US",
        company_name="Latecomer Inc.",
        event_date=utcnow() + timedelta(days=3),
        source_url="https://sec.gov/late.htm",
        payload={"code": "LATE", "profile": {"checked": True}},  # no description yet
    )
    db_session.add(event)
    await db_session.flush()
    db_session.add(
        Notification(
            event_id=event.id,
            channel="telegram",
            dedup_key="dk-notif-late-7",
            reminder_window="7",
            status="pending",
            payload={"text": "stale"},
        )
    )
    await db_session.commit()

    client = RecorderClient(message_id=55501)
    out = await deliver_pending_notifications(db_session, client=client)
    assert out.sent == 1
    sent_text = client.calls[0][1]
    assert "blockquote" not in sent_text  # shipped description-less

    # Description lands from a later enrich run; backfill edits the message.
    event.payload = {
        **event.payload,
        "profile": {"checked": True, "description": "Latecomer builds X."},
    }
    db_session.add(event)
    await db_session.commit()

    edited = await backfill_alert_edits(db_session, [event], client=client)
    assert edited == 1
    assert client.edits and client.edits[0]["message_id"] == 55501
    assert "blockquote" in client.edits[0]["text"]
    # The stored snapshot now matches what the user sees after the edit.
    row = (
        await db_session.execute(
            select(Notification).where(Notification.dedup_key == "dk-notif-late-7")
        )
    ).scalar_one()
    assert "blockquote" in row.payload["text"]


# ---------------------------------------------------------------------------
# failure handling — SendResult(ok=False) and raising clients
# ---------------------------------------------------------------------------


async def test_failed_send_marked_failed_with_error_recorded(
    db_session: AsyncSession,
) -> None:
    db_session.add(TelegramChat(chat_id="111", is_active=True))
    db_session.add(_notification("evt:fail:7", "doomed alert"))
    await db_session.commit()

    out = await deliver_pending_notifications(
        db_session, client=RecorderClient(ok=False)
    )
    assert (out.sent, out.failed, out.skipped) == (0, 1, 0)

    row = (
        await db_session.execute(
            select(Notification).where(Notification.dedup_key == "evt:fail:7")
        )
    ).scalar_one()
    assert row.status == "failed"  # retryable: dispatch re-selects "failed"
    assert row.error == "blocked"
    assert row.attempts == 1
    assert row.sent_at is None


async def test_raising_send_marks_failed_and_processes_remaining(
    db_session: AsyncSession,
) -> None:
    """A client that RAISES (contract violation / malformed payload) must not
    abort the run: the poison notification is marked failed with the error
    recorded, and later pending notifications are still delivered."""
    db_session.add(TelegramChat(chat_id="111", is_active=True))
    db_session.add(
        _notification(
            "evt:poison:7",
            "POISON",
            created_at=utcnow() - timedelta(minutes=5),  # processed first
        )
    )
    db_session.add(_notification("evt:healthy:7", "healthy alert"))
    await db_session.commit()

    client = RecorderClient(raise_for_texts={"POISON"})
    out = await deliver_pending_notifications(db_session, client=client)
    assert (out.sent, out.failed, out.skipped) == (1, 1, 0)

    poison = (
        await db_session.execute(
            select(Notification).where(Notification.dedup_key == "evt:poison:7")
        )
    ).scalar_one()
    assert poison.status == "failed"  # stays retryable
    assert poison.error is not None and "RuntimeError" in poison.error
    assert poison.attempts == 1

    healthy = (
        await db_session.execute(
            select(Notification).where(Notification.dedup_key == "evt:healthy:7")
        )
    ).scalar_one()
    assert healthy.status == "sent"
    assert ("111", "healthy alert") in client.calls


async def test_crash_before_send_loop_still_counts_attempt(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash BEFORE the send loop (event load / formatter re-render) must
    still increment ``attempts`` — otherwise a deterministically-poison row
    reports attempts=0 forever while being retried on every run."""
    db_session.add(TelegramChat(chat_id="111", is_active=True))
    db_session.add(_notification("evt:preboom:7", "alert", event_id=12345))
    await db_session.commit()

    class _BoomRepo:
        def __init__(self, session: AsyncSession) -> None:
            pass

        async def get(self, event_id: int) -> None:
            raise RuntimeError("event load exploded")

    monkeypatch.setattr(
        "catalyst_radar.services.dispatch.EventRepository", _BoomRepo
    )

    client = RecorderClient()
    out = await deliver_pending_notifications(db_session, client=client)
    assert (out.sent, out.failed, out.skipped) == (0, 1, 0)
    assert client.calls == []  # crashed before any send

    row = (
        await db_session.execute(
            select(Notification).where(Notification.dedup_key == "evt:preboom:7")
        )
    ).scalar_one()
    assert row.status == "failed"
    assert row.error is not None and "RuntimeError" in row.error
    assert row.attempts == 1


async def test_partial_fanout_raise_preserves_delivered_chats(
    db_session: AsyncSession,
) -> None:
    """If the client raises midway through the chat fan-out, chats already
    delivered in this run must be persisted so the retry only targets the
    missing chat — 'Re-runs never double-send' (dispatch docstring)."""
    db_session.add_all(
        [
            TelegramChat(chat_id="111", is_active=True),
            TelegramChat(chat_id="222", is_active=True),
        ]
    )
    db_session.add(_notification("evt:partial:7", "fanout alert"))
    await db_session.commit()

    first = RecorderClient(raise_for_chats={"222"})
    out = await deliver_pending_notifications(db_session, client=first)
    assert (out.sent, out.failed) == (0, 1)

    row = (
        await db_session.execute(
            select(Notification).where(Notification.dedup_key == "evt:partial:7")
        )
    ).scalar_one()
    assert row.status == "failed"
    assert row.payload["delivered_chats"] == ["111"]

    retry = RecorderClient()
    out2 = await deliver_pending_notifications(db_session, client=retry)
    assert out2.sent == 1
    assert retry.calls == [("222", "fanout alert")]  # 111 NOT re-sent


# ---------------------------------------------------------------------------
# retry path
# ---------------------------------------------------------------------------


async def test_failed_notification_retried_on_next_run_and_succeeds(
    db_session: AsyncSession,
) -> None:
    db_session.add(TelegramChat(chat_id="900", is_active=True))
    db_session.add(_notification("evt:retry:30", "ipo reminder"))
    await db_session.commit()

    out1 = await deliver_pending_notifications(
        db_session, client=RecorderClient(ok=False)
    )
    assert out1.failed == 1

    ok = RecorderClient()
    out2 = await deliver_pending_notifications(db_session, client=ok)
    assert (out2.sent, out2.failed) == (1, 0)
    assert ok.calls == [("900", "ipo reminder")]

    row = (
        await db_session.execute(
            select(Notification).where(Notification.dedup_key == "evt:retry:30")
        )
    ).scalar_one()
    assert row.status == "sent"
    assert row.error is None  # cleared on successful retry
    assert row.sent_at is not None
    assert row.attempts == 2  # one per dispatch run


# ---------------------------------------------------------------------------
# kill-switch
# ---------------------------------------------------------------------------


async def test_alerts_disabled_sends_nothing_and_marks_nothing(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "telegram_alerts_enabled", False)
    db_session.add(TelegramChat(chat_id="111", is_active=True))
    db_session.add(_notification("evt:disabled:7", "muted alert"))
    await db_session.commit()

    client = RecorderClient()
    out = await deliver_pending_notifications(db_session, client=client)
    assert (out.sent, out.failed, out.skipped) == (0, 0, 0)
    assert client.calls == []

    row = (
        await db_session.execute(
            select(Notification).where(Notification.dedup_key == "evt:disabled:7")
        )
    ).scalar_one()
    assert row.status == "pending"  # untouched; delivers once re-enabled
    assert row.attempts == 0


# ---------------------------------------------------------------------------
# NotificationService.deliver — message_id persistence + window idempotency
# ---------------------------------------------------------------------------


async def test_deliver_persists_telegram_message_id(
    db_session: AsyncSession,
) -> None:
    client = RecorderClient(message_id=424242)
    svc = NotificationService(db_session, client=client)  # type: ignore[arg-type]

    out = await svc.deliver(
        chat_id="55",
        text="IPO lists in 7 days",
        dedup_key="eodhd:ipo:ACME.US:7",
        reminder_window="7",
    )
    assert out.status == "sent"

    row = (
        await db_session.execute(
            select(Notification).where(Notification.dedup_key == "eodhd:ipo:ACME.US:7")
        )
    ).scalar_one()
    assert row.status == "sent"
    assert row.telegram_message_id == 424242
    assert row.sent_at is not None
    assert row.reminder_window == "7"


async def test_same_event_and_window_never_sends_twice(
    db_session: AsyncSession,
) -> None:
    client = RecorderClient()
    svc = NotificationService(db_session, client=client)  # type: ignore[arg-type]
    key = "eodhd:ipo:ACME.US:1"  # same event + same reminder window

    first = await svc.deliver(chat_id="55", text="lists tomorrow", dedup_key=key)
    second = await svc.deliver(chat_id="55", text="lists tomorrow", dedup_key=key)

    assert first.status == "sent"
    assert second.status == "skipped"
    assert len(client.calls) == 1  # exactly one Telegram send
    assert await _count(db_session, key) == 1  # exactly one row, not two


async def test_deliver_failure_is_retryable_on_same_row(
    db_session: AsyncSession,
) -> None:
    key = "eodhd:earnings:ACME.US:0"
    failing = NotificationService(db_session, client=RecorderClient(ok=False))  # type: ignore[arg-type]
    out = await failing.deliver(chat_id="55", text="results today", dedup_key=key)
    assert out.status == "failed"
    assert out.detail == "blocked"

    ok_client = RecorderClient(message_id=99)
    retrying = NotificationService(db_session, client=ok_client)  # type: ignore[arg-type]
    out2 = await retrying.deliver(chat_id="55", text="results today", dedup_key=key)
    assert out2.status == "sent"
    assert out2.notification_id == out.notification_id  # same row, not a new one

    row = (
        await db_session.execute(
            select(Notification).where(Notification.dedup_key == key)
        )
    ).scalar_one()
    assert row.telegram_message_id == 99
    assert row.error is None
    assert row.attempts == 2
    assert await _count(db_session, key) == 1
