"""Self-healing of already-sent IPO alerts.

When an IPO alert ships to Telegram before its business description
(``payload['profile']['description']``) lands, a later enrich run edits
the already-sent message in place via ``editMessageText`` so the user's
existing alert grows the blurb instead of staying empty.

These exercise ``services.alert_backfill.backfill_alert_edits`` directly
with fabricated Event + Notification rows, using a recorder Telegram
stub (no network). The dispatch render+keyboard logic is reused via the
shared ``render_ipo_alert`` helper so an edited message stays identical
to a freshly-sent one.
"""

from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.models.base import utcnow
from catalyst_radar.models.event import Event
from catalyst_radar.models.notification import Notification
from catalyst_radar.services.alert_backfill import backfill_alert_edits
from catalyst_radar.services.alerts import format_ipo
from catalyst_radar.services.telegram_client import SendResult


class EditRecorder:
    """Telegram stub: records every edit_message_text call."""

    configured = True

    def __init__(self, *, ok: bool = True, raise_always: bool = False) -> None:
        self.ok = ok
        self.raise_always = raise_always
        self.calls: list[dict] = []

    async def edit_message_text(
        self,
        chat_id: str,
        message_id: int,
        text: str,
        *,
        reply_markup: dict | None = None,
    ) -> SendResult:
        self.calls.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "reply_markup": reply_markup,
            }
        )
        if self.raise_always:
            raise RuntimeError("telegram edit exploded")
        if not self.ok:
            return SendResult(ok=False, error_code=400, description="bad request")
        return SendResult(ok=True, message_id=message_id)


async def _make_ipo_event(
    session: AsyncSession, *, sid: str, with_description: bool
) -> Event:
    profile: dict = {"checked": True, "currency": "USD"}
    if with_description:
        profile["description"] = "Acme builds industrial robots for warehouses."
    event = Event(
        event_type="ipo",
        source_name="eodhd.ipos",
        source_event_id=sid,
        dedup_key=f"dk-event-{sid}",
        symbol="ACME",
        country="US",
        company_name="Acme Robotics Inc.",
        event_date=utcnow() + timedelta(days=3),
        source_url="https://sec.gov/acme.htm",
        payload={"code": "ACME", "offer_price": "12", "profile": profile},
    )
    session.add(event)
    await session.commit()
    await session.refresh(event)
    return event


async def _make_sent_notification(
    session: AsyncSession, event: Event, *, stale_text: str, window: str = "7"
) -> Notification:
    n = Notification(
        event_id=event.id,
        channel="telegram",
        chat_id="123",
        dedup_key=f"dk-notif-{event.source_event_id}-{window}",
        reminder_window=window,
        status="sent",
        telegram_message_id=4242,
        sent_at=utcnow(),
        payload={"text": stale_text, "delivered_chats": ["123"]},
    )
    session.add(n)
    await session.commit()
    await session.refresh(n)
    return n


async def test_backfill_edits_sent_message_with_new_description(
    db_session: AsyncSession,
) -> None:
    # Event sent BEFORE the description landed: stored text has no blurb.
    event = await _make_ipo_event(db_session, sid="s1", with_description=False)
    stale_text = format_ipo(event)
    n = await _make_sent_notification(db_session, event, stale_text=stale_text)

    # Description lands later.
    payload = dict(event.payload or {})
    payload["profile"] = {**payload["profile"], "description": "Acme builds robots."}
    event.payload = payload
    db_session.add(event)
    await db_session.commit()

    client = EditRecorder()
    await backfill_alert_edits(db_session, [event], client=client)

    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["chat_id"] == "123"
    assert call["message_id"] == 4242
    assert "Acme builds robots." in call["text"]
    # Keyboard must be carried so editMessageText doesn't drop the inline kb.
    assert call["reply_markup"] is not None
    assert "inline_keyboard" in call["reply_markup"]

    # Stored snapshot is updated to the freshly-rendered text.
    await db_session.refresh(n)
    assert "Acme builds robots." in (n.payload or {})["text"]


async def test_backfill_is_idempotent_when_text_unchanged(
    db_session: AsyncSession,
) -> None:
    event = await _make_ipo_event(db_session, sid="s2", with_description=True)
    # Stored text already matches the current render — nothing to do.
    n = await _make_sent_notification(db_session, event, stale_text=format_ipo(event))

    client = EditRecorder()
    await backfill_alert_edits(db_session, [event], client=client)

    assert client.calls == []
    await db_session.refresh(n)
    assert (n.payload or {})["text"] == format_ipo(event)


async def test_backfill_skips_event_without_sent_notification(
    db_session: AsyncSession,
) -> None:
    event = await _make_ipo_event(db_session, sid="s3", with_description=True)
    # Only a pending notification exists — not editable.
    pending = Notification(
        event_id=event.id,
        channel="telegram",
        chat_id="123",
        dedup_key="dk-notif-pending",
        status="pending",
        payload={"text": "old"},
    )
    db_session.add(pending)
    await db_session.commit()

    client = EditRecorder()
    await backfill_alert_edits(db_session, [event], client=client)

    assert client.calls == []


async def test_backfill_edits_every_sent_reminder_window(
    db_session: AsyncSession,
) -> None:
    event = await _make_ipo_event(db_session, sid="s4", with_description=False)
    stale_text = format_ipo(event)
    await _make_sent_notification(db_session, event, stale_text=stale_text, window="14")
    await _make_sent_notification(db_session, event, stale_text=stale_text, window="7")

    payload = dict(event.payload or {})
    payload["profile"] = {**payload["profile"], "description": "Acme builds robots."}
    event.payload = payload
    db_session.add(event)
    await db_session.commit()

    client = EditRecorder()
    await backfill_alert_edits(db_session, [event], client=client)

    assert len(client.calls) == 2
    assert all("Acme builds robots." in c["text"] for c in client.calls)


async def test_backfill_edit_failure_does_not_propagate(
    db_session: AsyncSession,
) -> None:
    event = await _make_ipo_event(db_session, sid="s5", with_description=False)
    stale_text = format_ipo(event)
    n = await _make_sent_notification(db_session, event, stale_text=stale_text)

    payload = dict(event.payload or {})
    payload["profile"] = {**payload["profile"], "description": "Acme builds robots."}
    event.payload = payload
    db_session.add(event)
    await db_session.commit()

    client = EditRecorder(raise_always=True)
    # Must not raise — enrichment-style best-effort call still completes.
    await backfill_alert_edits(db_session, [event], client=client)

    assert len(client.calls) == 1
    # Stored text stays stale (edit never succeeded) — no false self-heal.
    await db_session.refresh(n)
    assert (n.payload or {})["text"] == stale_text


async def test_backfill_ignores_non_ipo_and_descriptionless_events(
    db_session: AsyncSession,
) -> None:
    # IPO without a description in its profile → nothing to backfill.
    event = await _make_ipo_event(db_session, sid="s6", with_description=False)
    await _make_sent_notification(db_session, event, stale_text=format_ipo(event))

    client = EditRecorder()
    await backfill_alert_edits(db_session, [event], client=client)

    assert client.calls == []


class _FlakyEditRecorder(EditRecorder):
    """Fails edits for one chat, succeeds for the rest."""

    def __init__(self, fail_chat: str) -> None:
        super().__init__()
        self.fail_chat = fail_chat

    async def edit_message_text(self, chat_id, message_id, text, *, reply_markup=None):
        res = await super().edit_message_text(
            chat_id, message_id, text, reply_markup=reply_markup
        )
        if chat_id == self.fail_chat:
            return SendResult(ok=False, error_code=400, description="bad request")
        return res


async def _dispatch_to_chats(db_session: AsyncSession, event: Event, chat_ids: list[str]):
    """Fan an alert for ``event`` out to ``chat_ids`` via the real dispatcher.
    Returns chat_id -> the message id Telegram assigned that chat's copy."""
    from catalyst_radar.models.notification import TelegramChat
    from catalyst_radar.services.dispatch import deliver_pending_notifications
    from tests.test_telegram import FakeTelegramClient

    db_session.add_all([TelegramChat(chat_id=c, is_active=True) for c in chat_ids])
    db_session.add(
        Notification(
            event_id=event.id,
            channel="telegram",
            dedup_key=f"dk-fanout-{event.source_event_id}",
            status="pending",
            payload={"text": "x"},
        )
    )
    await db_session.commit()
    sender = FakeTelegramClient()
    await deliver_pending_notifications(db_session, client=sender)
    # FakeTelegramClient assigns message_id = send order (1-based).
    return {cid: i + 1 for i, (cid, _) in enumerate(sender.sent)}


async def _land_description(db_session: AsyncSession, event: Event) -> None:
    payload = dict(event.payload or {})
    payload["profile"] = {**payload["profile"], "description": "Acme builds robots."}
    event.payload = payload
    db_session.add(event)
    await db_session.commit()


async def test_backfill_edits_every_chats_copy(db_session: AsyncSession) -> None:
    event = await _make_ipo_event(db_session, sid="fan1", with_description=False)
    sent_ids = await _dispatch_to_chats(db_session, event, ["111", "222", "333"])
    await _land_description(db_session, event)

    client = EditRecorder()
    edited = await backfill_alert_edits(db_session, [event], client=client)

    assert edited == 3
    assert {c["chat_id"]: c["message_id"] for c in client.calls} == sent_ids
    assert all("Acme builds robots." in c["text"] for c in client.calls)

    # Idempotent: every copy already shows the render.
    again = EditRecorder()
    assert await backfill_alert_edits(db_session, [event], client=again) == 0
    assert again.calls == []


async def test_backfill_retries_only_the_chat_whose_edit_failed(
    db_session: AsyncSession,
) -> None:
    event = await _make_ipo_event(db_session, sid="fan2", with_description=False)
    await _dispatch_to_chats(db_session, event, ["111", "222"])
    await _land_description(db_session, event)

    flaky = _FlakyEditRecorder(fail_chat="222")
    assert await backfill_alert_edits(db_session, [event], client=flaky) == 1

    retry = EditRecorder()
    assert await backfill_alert_edits(db_session, [event], client=retry) == 1
    assert [c["chat_id"] for c in retry.calls] == ["222"]
