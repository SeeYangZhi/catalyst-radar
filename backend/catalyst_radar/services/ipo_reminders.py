"""Listing-day reminder for starred IPOs.

A starred IPO earns one tagged "listing today" push on its listing date,
sent only to the chat(s) that starred it. This runs inside the existing
hourly ``catalyst_radar.digests`` task and fires at or after
``daily_digest_hour`` (local tz), once per chat — the per-(event, chat)
``day_of_notified_at`` marker guarantees once-ever
even though the hour gate uses ``>=`` (robust to a missed exact hour).
Star wins over dismiss: a dismissed-but-starred IPO still reminds.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.logging import get_logger
from catalyst_radar.models.base import now_local
from catalyst_radar.models.notification import TelegramChat
from catalyst_radar.repositories.event_flags_repository import EventFlagsRepository
from catalyst_radar.repositories.notification_repository import TelegramChatRepository
from catalyst_radar.services.alerts import format_ipo
from catalyst_radar.services.telegram_client import TelegramClient

log = get_logger(__name__)

_BANNER = "⭐ <b>Listing today</b>"


def _tag(chat: TelegramChat) -> str:
    """Literal ``@username`` tag for this chat's user, or "" when the chat has
    no captured username. Telegram auto-links the handle, so the recipient
    sees a real mention of themselves."""
    handle = (chat.username or "").strip().lstrip("@")
    return f" @{handle}" if handle else ""


@dataclass(slots=True)
class DayOfSummary:
    reminded: int  # (chat, event) reminders delivered
    skipped: str | None = None


async def run_day_of_reminders(
    session: AsyncSession,
    client: TelegramClient | None = None,
    now: datetime | None = None,
) -> DayOfSummary:
    """Send each chat the listing-day reminder for every IPO *it* starred
    that lists today, once per chat. ``now`` is injectable for tests; it defaults to the configured
    alert timezone."""
    from catalyst_radar.runtime_config import effective

    cfg = await effective(session)
    if not cfg.telegram_alerts_enabled:
        return DayOfSummary(0, skipped="telegram_alerts_disabled")

    client = client or TelegramClient()
    if not client.configured:
        return DayOfSummary(0, skipped="telegram_not_configured")

    chats = {c.chat_id: c for c in await TelegramChatRepository(session).recipients()}
    if not chats:
        return DayOfSummary(0, skipped="no_active_chats")

    now = now or now_local(cfg.alert_timezone)
    if now.hour < int(cfg.daily_digest_hour):
        return DayOfSummary(0, skipped="before_digest_hour")

    repo = EventFlagsRepository(session)
    reminded = 0
    # Stars are per chat: each chat is reminded only of IPOs it starred.
    for chat_id, ev in await repo.due_day_of(now.date(), cfg.alert_timezone):
        chat = chats.get(chat_id)
        if chat is None:
            continue  # unsubscribed chat: leave unmarked in case it re-/starts
        res = await client.send_message(chat_id, f"{_BANNER}{_tag(chat)}\n{format_ipo(ev)}")
        if res.ok:
            reminded += 1
        else:
            log.warning("day_of_send_failed", chat_id=chat_id, error=res.description)
        # Mark regardless of outcome so a persistently failing chat never
        # causes repeated re-sends on every hourly tick.
        await repo.mark_day_of_notified(ev.id, chat_id)

    log.info("day_of_reminders", reminded=reminded)
    return DayOfSummary(reminded)
