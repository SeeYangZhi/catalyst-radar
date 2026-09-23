"""Self-heal already-sent IPO alerts when their description lands late.

The dispatcher re-renders an IPO alert at SEND time, so a description
that arrives before dispatch is included automatically. This module
covers the other case: a message was ALREADY sent (description-less),
and a later enrich run filled in ``payload['profile']['description']``.
Rather than spam a second alert, we EDIT the existing Telegram message
in place via ``editMessageText`` so the user's original alert grows the
blurb.

Shared by the three IPO enrichers (US/HK/CN); each tail-calls
``backfill_alert_edits`` after committing the profile. The call is
strictly best-effort — a Telegram failure (or a contract-violating
raise) must never abort the enrich run or undo the committed profile.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.logging import get_logger
from catalyst_radar.models.event import Event
from catalyst_radar.models.notification import Notification
from catalyst_radar.repositories.event_flags_repository import EventFlagsRepository
from catalyst_radar.repositories.notification_repository import (
    NotificationRepository,
)
from catalyst_radar.services.dispatch import render_ipo_alert
from catalyst_radar.services.telegram_client import TelegramClient

log = get_logger(__name__)


def _has_description(event: Event) -> bool:
    """True for an IPO event whose profile now carries a description —
    the only events that have something new to backfill onto an alert."""
    if event.event_type != "ipo":
        return False
    profile = (event.payload or {}).get("profile") or {}
    return bool(profile.get("description"))


def _message_targets(n: Notification) -> dict[str, int]:
    """``chat_id -> message_id`` for every delivered copy of this alert.
    Rows sent before per-chat ids were recorded carry only the first
    delivery in the singular columns."""
    ids = (n.payload or {}).get("message_ids") or {}
    targets = {str(k): int(v) for k, v in ids.items()}
    if n.chat_id is not None and n.telegram_message_id is not None:
        targets.setdefault(n.chat_id, n.telegram_message_id)
    return targets


async def backfill_alert_edits(
    session: AsyncSession,
    events: Iterable[Event],
    *,
    client: TelegramClient | None = None,
) -> int:
    """Edit already-sent IPO alerts in place for events whose description
    just landed. Returns the number of messages successfully edited.

    For each IPO event with a description, every delivered notification
    (one per reminder window) is re-rendered with the SAME text the
    dispatcher uses, and EVERY chat's copy whose last-shown text differs
    (``payload['chat_texts']``) is edited, with that chat's own star state
    on the keyboard. Copies already showing the render are skipped
    (idempotent per chat). Per-message failures are logged and swallowed
    so one bad edit never blocks the others or the enrich run that called
    us; a failed chat is retried on the next enrich pass."""
    candidates = [e for e in events if _has_description(e)]
    if not candidates:
        return 0

    client = client or TelegramClient()
    if not getattr(client, "configured", False):
        log.info("alert_backfill_skipped", reason="telegram_not_configured")
        return 0

    repo = NotificationRepository(session)
    edited = 0
    dirty = False
    for event in candidates:
        if event.id is None:
            continue
        try:
            notifications = await repo.get_sent_by_event(event.id)
        except Exception as exc:  # noqa: BLE001 - one event must not abort the sweep
            log.warning("alert_backfill_lookup_failed", event_id=event.id, error=repr(exc))
            continue

        text, _ = render_ipo_alert(event)
        if not text:
            continue
        starred_chats = await EventFlagsRepository(session).starred_chat_ids(event.id)

        for n in notifications:
            payload = dict(n.payload or {})
            chat_texts: dict[str, str] = dict(payload.get("chat_texts") or {})
            changed = False
            for chat_id, message_id in _message_targets(n).items():
                # Idempotent per chat: skip copies already showing this render.
                if chat_texts.get(chat_id, payload.get("text")) == text:
                    continue
                _, keyboard = render_ipo_alert(event, starred=chat_id in starred_chats)
                try:
                    res = await client.edit_message_text(
                        chat_id, message_id, text, reply_markup=keyboard
                    )
                except Exception as exc:  # noqa: BLE001 - a Telegram raise must not break enrich
                    log.warning(
                        "alert_backfill_edit_raised",
                        notification_id=n.id,
                        event_id=event.id,
                        chat_id=chat_id,
                        error=repr(exc),
                    )
                    continue
                if not res.ok:
                    log.warning(
                        "alert_backfill_edit_failed",
                        notification_id=n.id,
                        event_id=event.id,
                        chat_id=chat_id,
                        error=res.description,
                    )
                    continue
                chat_texts[chat_id] = text
                changed = True
                edited += 1
            if changed:
                payload["chat_texts"] = chat_texts
                payload["text"] = text
                n.payload = payload
                session.add(n)
                dirty = True

    if dirty:
        try:
            await session.commit()
        except Exception as exc:  # noqa: BLE001 - persistence failure stays best-effort
            log.warning("alert_backfill_commit_failed", error=repr(exc))
            await session.rollback()

    if edited:
        log.info("alert_backfill_ok", edited=edited)
    return edited
