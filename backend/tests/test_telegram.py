import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.config import settings
from catalyst_radar.models.company import TrackedCompany
from catalyst_radar.services.notification_service import NotificationService
from catalyst_radar.services.telegram_bot import handle_command, process_update
from catalyst_radar.services.telegram_client import SendResult


class FakeTelegramClient:
    configured = True

    def __init__(self, ok: bool = True, updates: list[dict] | None = None) -> None:
        self.ok = ok
        self.sent: list[tuple[str, str]] = []
        self.markups: list[dict | None] = []
        self._updates = updates or []

    async def get_updates(self, offset: int | None = None, poll_timeout: int = 0) -> list[dict]:
        return self._updates

    async def send_message(
        self,
        chat_id: str,
        text: str,
        *,
        reply_markup: dict | None = None,
        max_attempts: int = 3,
    ) -> SendResult:
        self.sent.append((chat_id, text))
        self.markups.append(reply_markup)
        if self.ok:
            return SendResult(ok=True, message_id=len(self.sent))
        return SendResult(ok=False, description="blocked")

    async def edit_message_text(
        self,
        chat_id: str,
        message_id: int,
        text: str,
        *,
        reply_markup: dict | None = None,
    ) -> SendResult:
        self.sent.append((chat_id, text))
        self.markups.append(reply_markup)
        return SendResult(ok=self.ok, message_id=message_id)

    async def answer_callback_query(
        self, callback_query_id: str, text: str | None = None
    ) -> bool:
        return True


async def test_notification_dedup_is_idempotent(
    db_session: AsyncSession,
) -> None:
    client = FakeTelegramClient()
    svc = NotificationService(db_session, client=client)  # type: ignore[arg-type]

    first = await svc.deliver(chat_id="123", text="hi", dedup_key="evt:1:window7")
    second = await svc.deliver(chat_id="123", text="hi", dedup_key="evt:1:window7")

    assert first.status == "sent"
    assert second.status == "skipped"
    assert len(client.sent) == 1  # not sent twice for same dedup key


async def test_failed_send_is_retryable(db_session: AsyncSession) -> None:
    failing = FakeTelegramClient(ok=False)
    svc = NotificationService(db_session, client=failing)  # type: ignore[arg-type]
    out = await svc.deliver(chat_id="1", text="x", dedup_key="evt:2")
    assert out.status == "failed"

    ok = FakeTelegramClient(ok=True)
    svc2 = NotificationService(db_session, client=ok)  # type: ignore[arg-type]
    retry = await svc2.deliver(chat_id="1", text="x", dedup_key="evt:2")
    assert retry.status == "sent"  # same dedup key, retried after failure


async def test_start_registers_chat_and_watchlist(
    db_session: AsyncSession,
) -> None:
    reply = await handle_command(db_session, "555", "/start")
    assert "registered" in reply.text.lower()
    assert reply.reply_markup is not None  # menu keyboard attached

    db_session.add(
        TrackedCompany(symbol="0700", exchange="HK", company_name="Tencent", source="manual")
    )
    await db_session.commit()

    wl = await handle_command(db_session, "555", "/watchlist")
    assert "Tencent" in wl.text
    assert "0700" in wl.text


async def test_help_and_unknown(db_session: AsyncSession) -> None:
    assert "/watchlist" in (await handle_command(db_session, "1", "/help")).text
    assert "Unknown command" in (await handle_command(db_session, "1", "/wat")).text


def test_username_helper() -> None:
    from catalyst_radar.services.telegram_bot import _username

    assert _username({"username": "alice_example"}) == "alice_example"
    assert _username({"first_name": "Alice"}) is None  # no public username
    assert _username({}) is None
    assert _username(None) is None


async def test_process_update_captures_username(db_session: AsyncSession) -> None:
    from catalyst_radar.repositories.notification_repository import (
        TelegramChatRepository,
    )

    update = {
        "update_id": 77,
        "message": {
            "chat": {"id": 555, "type": "private"},
            "text": "/start",
            "from": {"id": 555, "first_name": "Alice", "username": "alice_example"},
        },
    }
    await process_update(db_session, update)
    chat = await TelegramChatRepository(db_session).get_by_chat_id("555")
    assert chat is not None
    assert chat.username == "alice_example"


async def test_process_update_dedupes_by_update_id(
    db_session: AsyncSession,
) -> None:
    update = {
        "update_id": 10,
        "message": {"chat": {"id": 999}, "text": "/help"},
    }
    first = await process_update(db_session, update)
    assert first is not None and "/watchlist" in first.text

    again = await process_update(db_session, update)
    assert again is None  # same update_id ignored


async def test_dispatch_fans_out_to_all_chats_idempotently(
    db_session: AsyncSession,
) -> None:
    from catalyst_radar.models.notification import Notification, TelegramChat
    from catalyst_radar.services.dispatch import deliver_pending_notifications

    db_session.add_all(
        [
            TelegramChat(chat_id="111", is_active=True),
            TelegramChat(chat_id="222", is_active=True),
            TelegramChat(chat_id="333", is_active=False),  # inactive: skipped
        ]
    )
    db_session.add(
        Notification(
            channel="telegram",
            dedup_key="evt:fanout:7",
            status="pending",
            payload={"text": "IPO alert"},
        )
    )
    await db_session.commit()

    client = FakeTelegramClient()
    s1 = await deliver_pending_notifications(db_session, client=client)
    assert s1.sent == 1
    # delivered to both active chats, not the inactive one
    assert sorted(c for c, _ in client.sent) == ["111", "222"]

    # Now marked "sent"; a re-run does not reprocess or re-send it.
    s2 = await deliver_pending_notifications(db_session, client=client)
    assert (s2.sent, s2.failed, s2.skipped) == (0, 0, 0)
    assert len(client.sent) == 2  # no re-send


async def test_dispatch_respects_dispatch_after_grace_window(
    db_session: AsyncSession,
) -> None:
    """A pending notification with dispatch_after in the future must NOT
    be picked up by the dispatcher. This is how the UI's optimistic-undo
    toast can safely recall a Send: the row is queued but gated until the
    grace window passes."""
    from datetime import timedelta

    from catalyst_radar.models.base import utcnow
    from catalyst_radar.models.notification import Notification, TelegramChat
    from catalyst_radar.services.dispatch import deliver_pending_notifications

    db_session.add(TelegramChat(chat_id="555", is_active=True))
    future = utcnow() + timedelta(seconds=30)
    past = utcnow() - timedelta(seconds=30)
    db_session.add_all(
        [
            Notification(
                channel="telegram",
                dedup_key="evt:gated:future",
                status="pending",
                payload={"text": "gated"},
                dispatch_after=future,
            ),
            Notification(
                channel="telegram",
                dedup_key="evt:gated:past",
                status="pending",
                payload={"text": "elapsed"},
                dispatch_after=past,
            ),
            Notification(
                channel="telegram",
                dedup_key="evt:gated:null",
                status="pending",
                payload={"text": "ungated"},
                dispatch_after=None,
            ),
        ]
    )
    await db_session.commit()

    client = FakeTelegramClient()
    summary = await deliver_pending_notifications(db_session, client=client)
    # Future-gated row stays put; elapsed + null both deliver.
    assert summary.sent == 2
    delivered_texts = sorted(t for _, t in client.sent)
    assert delivered_texts == ["elapsed", "ungated"]


async def test_dispatch_retries_failed_chat(db_session: AsyncSession) -> None:
    from catalyst_radar.models.notification import Notification, TelegramChat
    from catalyst_radar.services.dispatch import deliver_pending_notifications

    db_session.add(TelegramChat(chat_id="900", is_active=True))
    db_session.add(
        Notification(
            channel="telegram",
            dedup_key="evt:retry:1",
            status="pending",
            payload={"text": "earnings"},
        )
    )
    await db_session.commit()

    failing = await deliver_pending_notifications(db_session, client=FakeTelegramClient(ok=False))
    assert failing.failed == 1

    ok = FakeTelegramClient(ok=True)
    retry = await deliver_pending_notifications(db_session, client=ok)
    assert retry.sent == 1
    assert ok.sent == [("900", "earnings")]  # retried the failed chat


async def test_dispatch_rerenders_ipo_text_from_live_event(
    db_session: AsyncSession,
) -> None:
    """Enrichment runs separately from sync. If a CN IPO event picks up
    a description via cn_ipo_enrich AFTER its notification was created,
    dispatch must re-render the alert text from the live event payload —
    otherwise the user gets the stale "no description" card we saw with
    $301669 高特电子."""
    from datetime import UTC, datetime, timedelta

    from catalyst_radar.models.event import Event
    from catalyst_radar.models.notification import Notification, TelegramChat
    from catalyst_radar.services.dispatch import deliver_pending_notifications

    list_date = datetime.now(UTC) + timedelta(days=7)
    db_session.add(TelegramChat(chat_id="42", is_active=True))
    event = Event(
        event_type="ipo",
        source_name="akshare_ipo",
        source_event_id="sse:ipo:688635",
        dedup_key="sse:ipo:688635",
        symbol="688635",
        exchange="SSE",
        country="CN",
        company_name="长进光子",
        title="IPO: 长进光子",
        event_date=list_date,
        # Pretend enrich populated the description AFTER sync stored
        # the notification's stale text:
        payload={
            "profile": {
                "description": "Specialty optical fiber manufacturer.",
                "description_source": "prospectus",
            }
        },
    )
    db_session.add(event)
    await db_session.flush()
    db_session.add(
        Notification(
            event_id=event.id,
            channel="telegram",
            dedup_key="sse:ipo:688635:7",
            status="pending",
            payload={"text": "STALE alert without description"},
        )
    )
    await db_session.commit()

    client = FakeTelegramClient(ok=True)
    out = await deliver_pending_notifications(db_session, client=client)
    assert out.sent == 1
    assert len(client.sent) == 1
    sent_text = client.sent[0][1]
    # The fresh render must replace the stale stored text.
    assert "STALE" not in sent_text
    assert "Specialty optical fiber" in sent_text
    assert "688635" in sent_text


async def test_poll_telegram_processes_and_dedupes(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from catalyst_radar.services.telegram_bot import poll_telegram

    # Polling path: ensure no ambient webhook URL forces the no-op guard.
    monkeypatch.setattr(settings, "telegram_webhook_url", "")
    upd = [{"update_id": 77, "message": {"chat": {"id": 5}, "text": "/help"}}]
    client = FakeTelegramClient(updates=upd)

    n1 = await poll_telegram(db_session, client=client)
    assert n1 == 1
    assert client.sent and "/watchlist" in client.sent[0][1]

    # same update_id again -> deduped, no reply
    n2 = await poll_telegram(db_session, client=client)
    assert n2 == 0


async def test_poll_telegram_disabled(db_session: AsyncSession) -> None:
    from catalyst_radar.repositories.config_repository import ConfigRepository
    from catalyst_radar.services.telegram_bot import poll_telegram

    await ConfigRepository(db_session).set("telegram_polling_enabled", False)
    await db_session.commit()
    client = FakeTelegramClient(
        updates=[{"update_id": 9, "message": {"chat": {"id": 5}, "text": "/help"}}]
    )
    assert await poll_telegram(db_session, client=client) == 0
    assert client.sent == []


async def test_poll_telegram_noop_when_webhook_configured(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from catalyst_radar.services.telegram_bot import poll_telegram

    # Webhook + getUpdates are mutually exclusive; polling must not run.
    monkeypatch.setattr(settings, "telegram_webhook_url", "https://x.test/webhook")
    client = FakeTelegramClient(
        updates=[{"update_id": 1, "message": {"chat": {"id": 5}, "text": "/help"}}]
    )
    assert await poll_telegram(db_session, client=client) == 0
    assert client.sent == []


async def test_webhook_rejects_missing_or_wrong_secret(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "telegram_webhook_secret", "s3cret")
    upd = {"update_id": 1, "message": {"chat": {"id": 7}, "text": "/help"}}

    no_hdr = await client.post("/api/v1/telegram/webhook", json=upd)
    assert no_hdr.status_code == 403

    bad = await client.post(
        "/api/v1/telegram/webhook",
        json=upd,
        headers={"X-Telegram-Bot-Api-Secret-Token": "nope"},
    )
    assert bad.status_code == 403


async def test_webhook_accepts_valid_secret_and_processes(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "telegram_webhook_secret", "s3cret")
    resp = await client.post(
        "/api/v1/telegram/webhook",
        json={"update_id": 2, "message": {"chat": {"id": 7}, "text": "/help"}},
        headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret"},
    )
    assert resp.status_code == 200
    # No bot token in tests -> reply not sent, but the update is processed.
    assert resp.json()["status"] in {"ok", "ignored"}


async def test_callback_menu_edits_in_place(db_session: AsyncSession) -> None:
    update = {
        "update_id": 200,
        "callback_query": {
            "id": "cq1",
            "data": "m",
            "message": {"message_id": 42, "chat": {"id": 5}},
        },
    }
    action = await process_update(db_session, update)
    assert action is not None
    assert action.edit and action.edit_message_id == 42
    assert action.callback_query_id == "cq1"
    assert action.reply_markup is not None  # menu keyboard


async def test_callback_toggles_setting(db_session: AsyncSession) -> None:
    from catalyst_radar.repositories.config_repository import ConfigRepository
    from catalyst_radar.runtime_config import effective

    before = bool((await effective(db_session)).telegram_alerts_enabled)
    update = {
        "update_id": 201,
        "callback_query": {
            "id": "cq2",
            "data": "st:telegram_alerts_enabled",
            "message": {"message_id": 7, "chat": {"id": 5}},
        },
    }
    action = await process_update(db_session, update)
    assert action is not None and action.answer_text
    stored = await ConfigRepository(db_session).get("telegram_alerts_enabled")
    assert stored == ("false" if before else "true")


def _kb_datas(kb: dict | None) -> list[str | None]:
    if not kb:
        return []
    return [b.get("callback_data") for row in kb["inline_keyboard"] for b in row]


def test_render_ipo_alert_carries_star_button() -> None:
    from datetime import UTC, datetime

    from catalyst_radar.models.event import Event
    from catalyst_radar.services.dispatch import render_ipo_alert

    ev = Event(
        id=7,
        event_type="ipo",
        source_name="t",
        source_event_id="r7",
        dedup_key="r7",
        symbol="AAA",
        exchange="US",
        company_name="Alpha",
        title="IPO: Alpha",
        event_date=datetime.now(UTC),
        payload={"profile": {"description": "A widgets maker."}},
    )
    text, kb = render_ipo_alert(ev, starred=False)
    assert text and "sr:7" in _kb_datas(kb)
    _, kb_starred = render_ipo_alert(ev, starred=True)
    labels = [b["text"] for row in kb_starred["inline_keyboard"] for b in row]
    assert "★ Starred ✓" in labels


async def test_dispatch_passes_star_state_to_keyboard(db_session: AsyncSession) -> None:
    from datetime import UTC, datetime, timedelta

    from catalyst_radar.models.event import Event
    from catalyst_radar.models.notification import Notification, TelegramChat
    from catalyst_radar.repositories.event_flags_repository import EventFlagsRepository
    from catalyst_radar.services.dispatch import deliver_pending_notifications

    db_session.add(TelegramChat(chat_id="42", is_active=True))
    ev = Event(
        event_type="ipo",
        source_name="t",
        source_event_id="star-disp",
        dedup_key="star-disp",
        symbol="BBB",
        exchange="US",
        company_name="Beta",
        title="IPO: Beta",
        event_date=datetime.now(UTC) + timedelta(days=3),
        payload={"profile": {"description": "Beta makes gadgets."}},
    )
    db_session.add(ev)
    await db_session.flush()
    await EventFlagsRepository(db_session).set_starred(ev.id, "42", True)
    db_session.add(
        Notification(
            event_id=ev.id,
            channel="telegram",
            dedup_key="star-disp:7",
            status="pending",
            payload={"text": "stale"},
        )
    )
    await db_session.commit()

    client = FakeTelegramClient(ok=True)
    out = await deliver_pending_notifications(db_session, client=client)
    assert out.sent == 1
    labels = [b["text"] for row in client.markups[-1]["inline_keyboard"] for b in row]
    assert "★ Starred ✓" in labels  # dispatcher honored the persisted star


async def _seed_ipo(db_session: AsyncSession, *, sid: str, days: int = 5):
    from datetime import UTC, datetime, timedelta

    from catalyst_radar.models.event import Event, EventRelevance

    ev = Event(
        event_type="ipo",
        source_name="t",
        source_event_id=sid,
        dedup_key=sid,
        symbol="ZZZ",
        exchange="US",
        company_name="Zeta",
        title="IPO: Zeta",
        event_date=datetime.now(UTC) + timedelta(days=days),
        payload={"profile": {"description": "Zeta builds rockets."}},
    )
    db_session.add(ev)
    await db_session.flush()
    db_session.add(EventRelevance(event_id=ev.id, matched=True))
    await db_session.commit()
    return ev


def _cb(data: str, *, mid: int = 1, uid: int = 1000):
    return {
        "update_id": uid,
        "callback_query": {
            "id": "cq",
            "data": data,
            "message": {"message_id": mid, "chat": {"id": 5}},
        },
    }


async def test_callback_star_toggles_and_rerenders_alert(db_session: AsyncSession) -> None:
    from catalyst_radar.repositories.event_flags_repository import EventFlagsRepository

    ev = await _seed_ipo(db_session, sid="cb-star")
    action = await process_update(db_session, _cb(f"sr:{ev.id}", uid=1001))
    assert action is not None and "Starred" in (action.answer_text or "")
    labels = [b["text"] for row in action.reply_markup["inline_keyboard"] for b in row]
    assert "★ Starred ✓" in labels
    assert (await EventFlagsRepository(db_session).get(ev.id, "5")).starred is True

    action2 = await process_update(db_session, _cb(f"sr:{ev.id}", uid=1002))
    assert "Star removed" in (action2.answer_text or "")
    assert (await EventFlagsRepository(db_session).get(ev.id, "5")).starred is False


async def test_callback_dismiss_then_undo(db_session: AsyncSession) -> None:
    from catalyst_radar.repositories.event_flags_repository import EventFlagsRepository

    ev = await _seed_ipo(db_session, sid="cb-dis")
    dis = await process_update(db_session, _cb(f"dx:{ev.id}", uid=1010))
    assert dis is not None and "Dismissed" in dis.text
    assert _kb_datas(dis.reply_markup) == [f"un:{ev.id}"]
    assert (await EventFlagsRepository(db_session).get(ev.id, "5")).dismissed is True

    undo = await process_update(db_session, _cb(f"un:{ev.id}", uid=1011))
    assert f"sr:{ev.id}" in _kb_datas(undo.reply_markup)
    assert (await EventFlagsRepository(db_session).get(ev.id, "5")).dismissed is False


async def test_starred_view_lists_starred_ipo(db_session: AsyncSession) -> None:
    from catalyst_radar.repositories.event_flags_repository import EventFlagsRepository

    ev = await _seed_ipo(db_session, sid="cb-view")
    await EventFlagsRepository(db_session).set_starred(ev.id, "5", True)
    action = await process_update(db_session, _cb("v:star:0", uid=1020))
    assert action is not None and "Starred" in action.text
    assert f"d:{ev.id}:star" in _kb_datas(action.reply_markup)


async def test_dismissed_event_hidden_from_ipo_list(db_session: AsyncSession) -> None:
    from catalyst_radar.repositories.event_flags_repository import EventFlagsRepository

    ev = await _seed_ipo(db_session, sid="cb-hide", days=10)
    await EventFlagsRepository(db_session).set_dismissed(ev.id, "5", True)
    view = await handle_command(db_session, "5", "/ipo")
    assert "Nothing for your watchlist" in view.text


async def test_callback_star_from_detail_preserves_kind(db_session: AsyncSession) -> None:
    from catalyst_radar.repositories.event_flags_repository import EventFlagsRepository

    ev = await _seed_ipo(db_session, sid="cb-sd")
    action = await process_update(db_session, _cb(f"sd:{ev.id}:ipo", uid=1031))
    assert action is not None and "Starred" in (action.answer_text or "")
    datas = _kb_datas(action.reply_markup)
    assert f"sd:{ev.id}:ipo" in datas  # still a detail card, kind preserved
    assert "v:ipo:0" in datas  # List button points back to the IPO list, not Starred
    assert (await EventFlagsRepository(db_session).get(ev.id, "5")).starred is True


async def test_ipo_list_not_clipped_at_25(db_session: AsyncSession) -> None:
    """The upcoming-IPO window can hold >25 matched listings across countries;
    the list view must not silently clip the latest-dated ones (regression:
    $03952 fell off the end at the old hard cap of 25)."""
    from datetime import UTC, datetime, timedelta

    from catalyst_radar.models.event import Event, EventRelevance
    from catalyst_radar.services.telegram_bot import _events_between

    base = datetime.now(UTC) + timedelta(days=1)
    for i in range(30):
        ev = Event(
            event_type="ipo",
            source_name="t",
            source_event_id=f"cap{i}",
            dedup_key=f"cap{i}",
            symbol=f"S{i:03d}",
            exchange="HKSE",
            company_name=f"Co {i}",
            title=f"IPO {i}",
            event_date=base + timedelta(days=i),
        )
        db_session.add(ev)
        await db_session.flush()
        db_session.add(EventRelevance(event_id=ev.id, matched=True))
    await db_session.commit()

    events = await _events_between(db_session, "5", "ipo", 90)
    assert len(events) == 30  # was clipped to 25 before the cap was raised
    assert events[-1].symbol == "S029"  # the latest-dated listing is present


async def test_admin_chat_ids_parse_comma_separated_list(monkeypatch) -> None:
    monkeypatch.setattr(settings, "telegram_admin_chat_id", " 111, 222 ,,")
    assert settings.telegram_admin_chat_ids == frozenset({"111", "222"})
    monkeypatch.setattr(settings, "telegram_admin_chat_id", "")
    assert settings.telegram_admin_chat_ids == frozenset()


async def test_dispatch_renders_each_chats_own_star_state(
    db_session: AsyncSession,
) -> None:
    from datetime import UTC, datetime, timedelta

    from catalyst_radar.models.event import Event
    from catalyst_radar.models.notification import Notification, TelegramChat
    from catalyst_radar.repositories.event_flags_repository import EventFlagsRepository
    from catalyst_radar.services.dispatch import deliver_pending_notifications

    db_session.add_all(
        [TelegramChat(chat_id="111", is_active=True), TelegramChat(chat_id="222", is_active=True)]
    )
    ev = Event(
        event_type="ipo",
        source_name="t",
        source_event_id="per-chat-star",
        dedup_key="per-chat-star",
        symbol="CCC",
        exchange="US",
        company_name="Gamma",
        title="IPO: Gamma",
        event_date=datetime.now(UTC) + timedelta(days=3),
    )
    db_session.add(ev)
    await db_session.flush()
    await EventFlagsRepository(db_session).set_starred(ev.id, "111", True)
    db_session.add(
        Notification(
            event_id=ev.id,
            channel="telegram",
            dedup_key="per-chat-star:7",
            status="pending",
            payload={"text": "x"},
        )
    )
    await db_session.commit()

    client = FakeTelegramClient()
    await deliver_pending_notifications(db_session, client=client)
    by_chat = {
        cid: [b["text"] for row in kb["inline_keyboard"] for b in row]
        for (cid, _), kb in zip(client.sent, client.markups, strict=True)
    }
    assert "★ Starred ✓" in by_chat["111"]
    assert "★ Starred ✓" not in by_chat["222"]


async def test_any_chat_can_subscribe_and_star_independently(
    db_session: AsyncSession, monkeypatch
) -> None:
    from catalyst_radar.repositories.event_flags_repository import EventFlagsRepository
    from catalyst_radar.repositories.notification_repository import (
        TelegramChatRepository,
    )

    monkeypatch.setattr(settings, "telegram_admin_chat_id", "111")
    monkeypatch.setattr(settings, "telegram_open_subscribe", True)
    start = {"update_id": 1, "message": {"chat": {"id": 333}, "text": "/start"}}
    assert await process_update(db_session, start) is not None
    assert await TelegramChatRepository(db_session).get_by_chat_id("333") is not None

    ev = await _seed_ipo(db_session, sid="indep")
    tap = {
        "update_id": 2,
        "callback_query": {
            "id": "cb1",
            "data": f"sr:{ev.id}",
            "message": {"chat": {"id": 333}, "message_id": 5},
        },
    }
    assert await process_update(db_session, tap) is not None
    repo = EventFlagsRepository(db_session)
    assert (await repo.get(ev.id, "333")).starred is True
    assert await repo.get(ev.id, "111") is None  # admin's state untouched


async def test_settings_toggles_are_admin_only(db_session: AsyncSession, monkeypatch) -> None:
    from catalyst_radar.runtime_config import effective
    from catalyst_radar.services.telegram_bot import handle_callback

    monkeypatch.setattr(settings, "telegram_admin_chat_id", "111")
    before = bool((await effective(db_session)).telegram_alerts_enabled)

    denied = await handle_callback(db_session, "st:telegram_alerts_enabled", "333")
    assert "Only admins" in (denied.answer_text or "")
    assert bool((await effective(db_session)).telegram_alerts_enabled) == before

    await handle_callback(db_session, "st:telegram_alerts_enabled", "111")
    assert bool((await effective(db_session)).telegram_alerts_enabled) != before


async def test_monitor_alerts_go_to_admin_chats_only(db_session: AsyncSession, monkeypatch) -> None:
    from catalyst_radar.models.notification import TelegramChat
    from catalyst_radar.repositories.notification_repository import (
        TelegramChatRepository,
    )

    monkeypatch.setattr(settings, "telegram_admin_chat_id", "111")
    db_session.add_all(
        [TelegramChat(chat_id="111", is_active=True), TelegramChat(chat_id="333", is_active=True)]
    )
    await db_session.commit()
    repo = TelegramChatRepository(db_session)
    assert [c.chat_id for c in await repo.admins()] == ["111"]
    assert sorted(c.chat_id for c in await repo.active()) == ["111", "333"]


async def test_closed_by_default_non_admin_is_ignored_and_gets_nothing(
    db_session: AsyncSession, monkeypatch
) -> None:
    from catalyst_radar.models.notification import TelegramChat
    from catalyst_radar.repositories.notification_repository import (
        TelegramChatRepository,
    )

    monkeypatch.setattr(settings, "telegram_admin_chat_id", "111")
    monkeypatch.setattr(settings, "telegram_open_subscribe", False)

    start = {"update_id": 1, "message": {"chat": {"id": 333}, "text": "/start"}}
    reply = await process_update(db_session, start)
    assert reply is not None and "private" in reply.text and "333" in reply.text
    repo = TelegramChatRepository(db_session)
    assert await repo.get_by_chat_id("333") is None  # not registered

    other = {"update_id": 2, "message": {"chat": {"id": 333}, "text": "/ipo"}}
    assert await process_update(db_session, other) is None

    # Even a stray active non-admin row receives nothing.
    db_session.add_all(
        [TelegramChat(chat_id="111", is_active=True), TelegramChat(chat_id="333", is_active=True)]
    )
    await db_session.commit()
    assert [c.chat_id for c in await repo.recipients()] == ["111"]

    monkeypatch.setattr(settings, "telegram_open_subscribe", True)
    assert sorted(c.chat_id for c in await repo.recipients()) == ["111", "333"]
