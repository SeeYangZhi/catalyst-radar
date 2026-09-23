from dataclasses import dataclass

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.logging import get_logger
from catalyst_radar.models.base import utcnow
from catalyst_radar.models.event import Event
from catalyst_radar.models.notification import Notification
from catalyst_radar.repositories.event_flags_repository import EventFlagsRepository
from catalyst_radar.repositories.event_repository import EventRepository
from catalyst_radar.repositories.notification_repository import (
    TelegramChatRepository,
)
from catalyst_radar.services.alerts import format_ipo
from catalyst_radar.services.telegram_client import TelegramClient
from catalyst_radar.services.telegram_ui import alert_kb

log = get_logger(__name__)


def render_ipo_alert(
    event: Event, *, starred: bool = False
) -> tuple[str | None, dict | None]:
    """Render an IPO event's pushed-alert text + inline keyboard.

    Single source of truth shared by the dispatcher (which re-renders at
    SEND time so a description that landed before dispatch is included)
    and the post-send backfill (``alert_backfill``, which EDITs an
    already-sent message once the description lands afterwards). Keeping
    both on one helper guarantees an edited message stays byte-identical
    to a freshly-sent one — including the keyboard, which ``editMessageText``
    drops unless it is re-supplied. ``starred`` reflects the event's current
    star flag so the toggle label is correct. Returns ``(None, kb)`` when the
    formatter yields nothing renderable."""
    text = format_ipo(event) or None
    keyboard = alert_kb(
        event_id=event.id,
        event_type=event.event_type,
        source_url=event.source_url,
        starred=starred,
    )
    return text, keyboard

# Serializes dispatch across the worker fleet. Every sync task
# (sync_ipos/sync_hk_ipos/sync_earnings/sync_catalysts) tail-calls
# deliver_pending_notifications, and Celery's prefork pool runs them
# concurrently — without this lock, each parallel call reads the same
# pending rows and fans out to Telegram before any of them commit "sent",
# causing N-way duplicate sends (observed: 4× at the 6h beat boundary).
_DISPATCH_LOCK_ID = 0x4452_5350  # "DRSP" — Dispatch Radar


@dataclass(slots=True)
class DispatchSummary:
    sent: int
    failed: int
    skipped: int


async def deliver_pending_notifications(
    session: AsyncSession, client: TelegramClient | None = None
) -> DispatchSummary:
    """Delivery is decoupled from generation. Each notification fans out
    to every active registered chat (multi-subscriber). Per-chat delivery
    is tracked in payload["delivered_chats"]; a notification is marked
    "sent" only once every currently-active chat has received it, and a
    partial/failed fan-out stays "failed" so the next run retries only the
    chats still missing it. Re-runs never double-send.
    """
    from catalyst_radar.runtime_config import effective

    cfg = await effective(session)
    if not cfg.telegram_alerts_enabled:
        log.info("dispatch_skipped", reason="telegram_alerts_disabled")
        return DispatchSummary(0, 0, 0)

    client = client or TelegramClient()
    if not client.configured:
        log.info("dispatch_skipped", reason="telegram_not_configured")
        return DispatchSummary(0, 0, 0)

    chats = await TelegramChatRepository(session).recipients()
    if not chats:
        log.info("dispatch_skipped", reason="no_active_chats")
        return DispatchSummary(0, 0, 0)
    chat_ids = [c.chat_id for c in chats]

    # pg_try_advisory_xact_lock auto-releases at commit/rollback, so the
    # lock is held for the entire send loop and concurrent dispatchers
    # short-circuit instead of re-sending. SQLite (tests) has no advisory
    # locks and runs single-threaded anyway, so we just skip the gate.
    if session.bind.dialect.name == "postgresql":
        locked = (
            await session.execute(
                text("SELECT pg_try_advisory_xact_lock(:k)"),
                {"k": _DISPATCH_LOCK_ID},
            )
        ).scalar_one()
        if not locked:
            log.info("dispatch_skipped", reason="another_dispatch_in_progress")
            return DispatchSummary(0, 0, 0)

    now = utcnow()
    result = await session.execute(
        select(Notification)
        .where(Notification.status.in_(["pending", "failed"]))
        .where(
            or_(
                Notification.dispatch_after.is_(None),
                Notification.dispatch_after <= now,
            )
        )
        .order_by(Notification.created_at)
        .limit(100)
    )
    pending = list(result.scalars().all())

    sent = failed = skipped = 0
    for n in pending:
        payload = dict(n.payload or {})
        delivered: set[str] = set(payload.get("delivered_chats", []))
        attempted = False
        try:
            targets = [cid for cid in chat_ids if cid not in delivered]
            if not targets:
                skipped += 1
                continue

            # Load the event once for both the keyboard and (for IPO alerts)
            # a re-render of the message text — enrichment runs separately
            # from sync, so the text captured at notification-creation time
            # can be stale (e.g. the prospectus description filled in by
            # cn_ipo_enrich a tick later was never reaching the alert).
            event = (
                await EventRepository(session).get(n.event_id)
                if n.event_id is not None
                else None
            )
            msg_text = payload.get("text")
            starred_chats: set[str] = set()
            if event is not None:
                # Flags are per chat, so each recipient's keyboard shows their
                # own ⭐/☆ state (rendered per target below).
                starred_chats = await EventFlagsRepository(session).starred_chat_ids(
                    event.id
                )
                fresh, _ = render_ipo_alert(event)
                if event.event_type == "ipo" and fresh:
                    msg_text = fresh
                    # Persist the re-rendered text so the stored snapshot
                    # matches what was actually sent. Otherwise `payload.text`
                    # remains the description-less version captured at
                    # notification-creation time, which is misleading when
                    # debugging "what did the user receive?".
                    payload["text"] = fresh
            if not msg_text:
                skipped += 1
                continue

            n.attempts += 1
            attempted = True
            any_failed = False
            last_error: str | None = None
            for cid in targets:
                reply_markup = (
                    render_ipo_alert(event, starred=cid in starred_chats)[1]
                    if event is not None
                    else None
                )
                res = await client.send_message(cid, msg_text, reply_markup=reply_markup)
                if res.ok:
                    delivered.add(cid)
                    # Per-chat message id + the exact text that chat received,
                    # so the late-description backfill can edit every copy.
                    # Written straight into payload so the poison-pill path
                    # below persists them too.
                    payload.setdefault("chat_texts", {})[cid] = msg_text
                    if res.message_id is not None:
                        payload.setdefault("message_ids", {})[cid] = res.message_id
                    # Persist the delivered message's id so a late-landing IPO
                    # description can self-heal this alert in place
                    # (alert_backfill / get_sent_by_event address the message by
                    # telegram_message_id; a NULL there makes the self-heal a
                    # silent no-op — the prod bug where raced alerts never grew
                    # their blurb). payload["message_ids"] holds every chat's id;
                    # the singular columns keep the first delivery (legacy rows
                    # have only these) — record once, don't clobber on retry.
                    if n.telegram_message_id is None and res.message_id is not None:
                        n.telegram_message_id = res.message_id
                        n.chat_id = cid
                else:
                    any_failed = True
                    last_error = res.description

            payload["delivered_chats"] = sorted(delivered)
            n.payload = payload
            if any_failed:
                n.status = "failed"
                n.error = last_error
                failed += 1
            else:
                n.status = "sent"
                n.sent_at = utcnow()
                n.error = None
                sent += 1
            session.add(n)
        except Exception as exc:
            # Poison-pill guard. send_message contractually returns
            # SendResult(ok=False) rather than raising, but a contract
            # violation — or a formatter blowing up on a malformed event
            # payload — must not abort the run: the SELECT orders by
            # created_at, so an aborting row would block every later
            # notification on every subsequent run. Mark it failed
            # (retryable) and persist the chats already delivered this
            # run so the retry only targets the missing ones (re-runs
            # never double-send).
            log.warning(
                "dispatch_notification_failed",
                notification_id=n.id,
                error=repr(exc),
            )
            if not attempted:
                # The crash happened before the send-loop increment (event
                # load or formatter), so count the attempt here too —
                # otherwise poison rows report attempts=0 forever.
                n.attempts += 1
            payload["delivered_chats"] = sorted(delivered)
            n.payload = payload
            n.status = "failed"
            n.error = repr(exc)
            failed += 1
            session.add(n)

    await session.commit()
    log.info("dispatch_done", sent=sent, failed=failed, skipped=skipped)
    return DispatchSummary(sent=sent, failed=failed, skipped=skipped)
