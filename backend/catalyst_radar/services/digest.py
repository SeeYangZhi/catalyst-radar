"""Scheduled daily / weekly digests pushed to Telegram.

A single beat task (``catalyst_radar.digests``) ticks hourly; this module decides
whether the current *local* hour matches the user-configured daily/weekly digest
time and, if so, broadcasts a summary of upcoming watchlist events to every
active chat. Idempotency markers in ``app_config`` prevent re-sending within the
same day / ISO-week, so the hourly tick is safe to run repeatedly.

The digest hour/day knobs are user-editable at runtime via the Settings page
(`daily_digest_hour`, `weekly_digest_day`, `weekly_digest_hour`); this is what
makes those knobs actually do something.
"""

import html
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.logging import get_logger
from catalyst_radar.models.base import now_local
from catalyst_radar.models.event import Event
from catalyst_radar.repositories.config_repository import ConfigRepository
from catalyst_radar.repositories.notification_repository import TelegramChatRepository
from catalyst_radar.services.telegram_bot import _event_button_label, _events_between
from catalyst_radar.services.telegram_client import TelegramClient

log = get_logger(__name__)

# app_config idempotency markers. Underscore-prefixed so they never collide with
# the user-editable runtime-config keys (which are validated against an allowlist).
_DAILY_LAST_KEY = "_digest_daily_last"  # ISO date, e.g. "2026-06-09"
_WEEKLY_LAST_KEY = "_digest_weekly_last"  # ISO year-week, e.g. "2026-W24"

_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


@dataclass(slots=True)
class DigestSummary:
    daily_sent: int  # active chats the daily digest reached this run
    weekly_sent: int
    skipped: str | None = None


def _weekday_index(spec: str) -> int | None:
    """Map a configured weekday ("MONDAY", "mon", "Tue"…) to 0=Mon..6=Sun."""
    return _WEEKDAYS.get(spec.strip()[:3].lower()) if spec else None


# Company name shown under the ticker. The digest has a whole line per name, so
# this can run longer than the button-label budget; clip very long names so a
# single entry can't wrap into a wall of text.
_NAME_MAX = 40


def _digest_line(e: Event) -> str:
    """One digest entry: the button-style ticker label, with the company name on
    its own indented line when present. ``_event_button_label`` is plain text
    (HTML-safe by construction); ``company_name`` is source data, so it is
    HTML-escaped and clipped before being dropped into the HTML message."""
    line = f"• {_event_button_label(e)}"
    name = (e.company_name or "").strip()
    # Skip the second line when the name is missing or just echoes the ticker.
    if name and name.lower() != (e.symbol or "").lower():
        if len(name) > _NAME_MAX:
            name = name[: _NAME_MAX - 1].rstrip() + "…"
        line += f"\n    <i>{html.escape(name)}</i>"
    return line


def _render(title: str, events: list[Event]) -> str:
    # _event_button_label is plain text (badge + ticker + date + relative day),
    # safe to drop into an HTML message without escaping; _digest_line escapes
    # the company name it appends.
    lines = [f"<b>{title}</b> <i>({len(events)})</i>", ""]
    lines += [_digest_line(e) for e in events]
    return "\n".join(lines)


async def _send_per_chat(
    session: AsyncSession,
    client: TelegramClient,
    chat_ids: list[str],
    title: str,
    days: int,
) -> int:
    """Render and send the digest per chat: each chat's own dismissals are
    excluded, and a chat with nothing left gets no message."""
    sent = 0
    for cid in chat_ids:
        events = await _events_between(session, cid, None, days)
        if not events:
            continue
        res = await client.send_message(cid, _render(title, events))
        if res.ok:
            sent += 1
        else:
            log.warning("digest_send_failed", chat_id=cid, error=res.description)
    return sent


async def run_digests(
    session: AsyncSession,
    client: TelegramClient | None = None,
    now: datetime | None = None,
) -> DigestSummary:
    """Send the daily and/or weekly digest if the current local time matches the
    configured schedule and it hasn't already gone out this day/ISO-week. `now`
    is injectable for tests; it defaults to the configured alert timezone."""
    from catalyst_radar.runtime_config import effective

    cfg = await effective(session)
    if not cfg.telegram_alerts_enabled:
        return DigestSummary(0, 0, skipped="telegram_alerts_disabled")

    client = client or TelegramClient()
    if not client.configured:
        return DigestSummary(0, 0, skipped="telegram_not_configured")

    chats = await TelegramChatRepository(session).recipients()
    if not chats:
        return DigestSummary(0, 0, skipped="no_active_chats")
    chat_ids = [c.chat_id for c in chats]

    now = now or now_local(cfg.alert_timezone)
    repo = ConfigRepository(session)
    daily_sent = weekly_sent = 0

    # Daily — today's watchlist events. Mark the day handled even when there's
    # nothing to send, so the hourly tick doesn't re-evaluate all day.
    if now.hour == int(cfg.daily_digest_hour):
        today = now.date().isoformat()
        if await repo.get(_DAILY_LAST_KEY) != today:
            daily_sent = await _send_per_chat(
                session, client, chat_ids, "Daily digest — today", 0
            )
            await repo.set(_DAILY_LAST_KEY, today)

    # Weekly — next 7 days, on the configured weekday + hour.
    wd = _weekday_index(cfg.weekly_digest_day)
    if wd is not None and now.weekday() == wd and now.hour == int(cfg.weekly_digest_hour):
        iso = now.isocalendar()
        wk = f"{iso.year}-W{iso.week:02d}"
        if await repo.get(_WEEKLY_LAST_KEY) != wk:
            weekly_sent = await _send_per_chat(
                session, client, chat_ids, "Weekly digest — next 7 days", 7
            )
            await repo.set(_WEEKLY_LAST_KEY, wk)

    log.info("digests_run", daily_sent=daily_sent, weekly_sent=weekly_sent)
    return DigestSummary(daily_sent, weekly_sent)
