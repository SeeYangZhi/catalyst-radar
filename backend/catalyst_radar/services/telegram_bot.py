from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.config import settings
from catalyst_radar.logging import get_logger
from catalyst_radar.models.base import utcnow
from catalyst_radar.models.event import Event, EventRelevance
from catalyst_radar.repositories.company_repository import (
    TrackedCompanyRepository,
)
from catalyst_radar.repositories.config_repository import ConfigRepository
from catalyst_radar.repositories.event_flags_repository import EventFlagsRepository
from catalyst_radar.repositories.event_repository import EventRepository
from catalyst_radar.repositories.notification_repository import (
    TelegramChatRepository,
)
from catalyst_radar.services.alerts import (
    _esc,
    _rel,
    _ticker,
    format_catalyst,
    format_earnings,
    format_ipo,
)
from catalyst_radar.services.dispatch import render_ipo_alert
from catalyst_radar.services.telegram_ui import (
    KINDS,
    PAGE_SIZE,
    TOGGLEABLE,
    BotAction,
    detail_kb,
    dismissed_kb,
    list_kb,
    menu_kb,
    settings_kb,
)

log = get_logger(__name__)

MENU_TEXT = (
    "<b>Catalyst Radar</b>\n"
    "<i>Earnings, IPOs &amp; market-moving catalysts for your watchlist.</i>\n"
    "Pick a view:"
)

HELP_TEXT = (
    "<b>Catalyst Radar</b>\n"
    "<i>Earnings, IPOs &amp; market-moving catalysts for your watchlist.</i>\n\n"
    "Use the buttons below, or these commands:\n"
    "/start — register this chat for alerts\n"
    "/watchlist — your tracked companies\n"
    "/settings — alert configuration &amp; toggles\n"
    "/today · /thisweek · /earnings · /ipo · /catalysts\n"
    "/help — show this message"
)

# Telegram Bot API command descriptors for setMyCommands
BOT_COMMANDS = [
    {"command": "start", "description": "Register this chat for alerts"},
    {"command": "help", "description": "Show all commands"},
    {"command": "watchlist", "description": "Your tracked companies"},
    {"command": "settings", "description": "Current alert configuration"},
    {"command": "today", "description": "Events today"},
    {"command": "thisweek", "description": "Next 7 days"},
    {"command": "earnings", "description": "Upcoming tracked earnings"},
    {"command": "ipo", "description": "Upcoming IPOs with detail"},
    {"command": "catalysts", "description": "Recent catalyst alerts"},
]

_LAST_UPDATE_KEY = "telegram_last_update_id"

_DETAIL = {
    "ipo": format_ipo,
    "earnings": format_earnings,
    "catalyst": format_catalyst,
}
_BADGE = {"ipo": "IPO", "earnings": "EPS", "catalyst": "CAT"}

# Upper bound on rows a single list view pulls. The menu paginates these
# (PAGE_SIZE per screen), so the cap only needs to cover a full window — and
# the 90-day IPO window across all enabled countries routinely exceeds the
# old hard cap of 25 (a busy HKSE/SSE/SZSE day alone can list 5-10), which
# silently clipped the latest-dated listings off the end of the view.
_LIST_LIMIT = 100


def _is_admin_chat(chat_id: str) -> bool:
    """Admin chats (``TELEGRAM_ADMIN_CHAT_ID``) may change global runtime
    config (settings toggles). Empty admin list = everyone is admin (dev
    convenience)."""
    admins = settings.telegram_admin_chat_ids
    return not admins or str(chat_id) in admins


def _may_use_bot(chat_id: str) -> bool:
    """Who may /start, browse and tap: anyone when open subscription is on,
    otherwise only the admin chats."""
    return settings.telegram_open_subscribe or _is_admin_chat(chat_id)


async def _events_between(
    session: AsyncSession, chat_id: str, event_type: str | None, days: int
) -> list[Event]:
    """Watchlist-relevant events only (matched to a tracked company /
    passing filters) within the date window. Excludes past events.
    Uses an IN-subquery so Postgres does not reject the ORDER BY
    under implicit distinctness."""
    now = utcnow()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    horizon = now + timedelta(days=days)
    matched_ids = (
        select(EventRelevance.event_id).where(EventRelevance.matched.is_(True)).scalar_subquery()
    )
    stmt = select(Event).where(
        Event.id.in_(matched_ids),
        Event.event_date.is_not(None),
        Event.event_date >= today_start,
        Event.event_date <= horizon,
    )
    dismissed = await EventFlagsRepository(session).dismissed_ids(chat_id)
    if dismissed:
        stmt = stmt.where(Event.id.not_in(dismissed))
    if event_type:
        stmt = stmt.where(Event.event_type == event_type)
    stmt = stmt.order_by(Event.event_date).limit(_LIST_LIMIT)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def _recent_events(
    session: AsyncSession, chat_id: str, event_type: str, lookback_days: int
) -> list[Event]:
    """Watchlist-relevant events from the recent past (newest first).
    Catalysts are dated when the news published, so they need a
    backward window, unlike forward-looking earnings/IPO."""
    now = utcnow()
    matched_ids = (
        select(EventRelevance.event_id).where(EventRelevance.matched.is_(True)).scalar_subquery()
    )
    dismissed = await EventFlagsRepository(session).dismissed_ids(chat_id)
    stmt = (
        select(Event)
        .where(
            Event.id.in_(matched_ids),
            Event.event_type == event_type,
            Event.event_date.is_not(None),
            Event.event_date >= now - timedelta(days=lookback_days),
            Event.event_date <= now,
        )
        .order_by(Event.event_date.desc())
        .limit(25)
    )
    if dismissed:
        stmt = stmt.where(Event.id.not_in(dismissed))
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def _load_kind(session: AsyncSession, chat_id: str, kind: str) -> list[Event]:
    if kind == "today":
        return await _events_between(session, chat_id, None, 0)
    if kind == "week":
        return await _events_between(session, chat_id, None, 7)
    if kind == "earn":
        return await _events_between(session, chat_id, "earnings", 45)
    if kind == "ipo":
        return await _events_between(session, chat_id, "ipo", 90)
    if kind == "cat":
        return await _recent_events(session, chat_id, "catalyst", 7)
    if kind == "star":
        return await EventFlagsRepository(session).starred_ipo_events(chat_id)
    return []


def _event_button_label(e: Event) -> str:
    """Plain-text (no HTML — Telegram button labels are literal)."""
    sym = f"${e.symbol}" if e.symbol else "?"
    when = (
        e.event_date.date().isoformat()
        if e.event_date and hasattr(e.event_date, "date")
        else "TBD"
    )
    badge = _BADGE.get(e.event_type, "")
    rel = _rel(e.event_date)
    label = f"[{badge}] {sym} · {when}"
    if rel:
        label += f" ({rel})"
    return label[:62]


# --- Screen builders. All return BotAction(edit=True): on a button tap the
# caller edits the message in place; on a slash command there is no message
# to edit so the caller falls back to sending a new one. ---


async def _menu_view() -> BotAction:
    return BotAction(text=MENU_TEXT, reply_markup=menu_kb(), edit=True)


async def _help_view() -> BotAction:
    return BotAction(text=HELP_TEXT, reply_markup=menu_kb(), edit=True)


async def _watchlist_view(session: AsyncSession) -> BotAction:
    companies = await TrackedCompanyRepository(session).list_active()
    if not companies:
        text = (
            "<b>Watchlist</b>\n"
            "<i>No tracked companies yet — add them in the dashboard.</i>"
        )
    else:
        rows = "\n".join(
            f"• <b>{_esc(c.company_name)}</b> "
            f"<code>{_ticker(c.symbol)}.{_esc(c.exchange)}</code>"
            for c in companies
        )
        text = f"<b>Watchlist</b> <i>({len(companies)})</i>\n{rows}"
    return BotAction(text=text, reply_markup=menu_kb(), edit=True)


async def _kind_view(session: AsyncSession, chat_id: str, kind: str, page: int) -> BotAction:
    title = "Starred IPOs" if kind == "star" else KINDS.get(kind, ("", kind))[1]
    events = await _load_kind(session, chat_id, kind)
    total = len(events)
    if total == 0:
        return BotAction(
            text=f"<b>{title}</b>\n<i>Nothing for your watchlist right now.</i>",
            reply_markup=menu_kb(),
            edit=True,
        )
    pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    page = max(0, min(page, pages - 1))
    chunk = events[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    items = [(e.id, _event_button_label(e)) for e in chunk]
    text = f"<b>{title}</b> <i>({total})</i>\n<i>Tap an item for full detail.</i>"
    if pages > 1:
        text += f"\nPage {page + 1}/{pages}"
    return BotAction(
        text=text,
        reply_markup=list_kb(items, kind, page, total),
        edit=True,
    )


async def _detail_view(
    session: AsyncSession, chat_id: str, event_id: int, kind: str
) -> BotAction:
    event = await EventRepository(session).get(event_id)
    if event is None:
        return BotAction(
            text="<i>Event no longer available.</i>",
            reply_markup=menu_kb(),
            edit=True,
        )
    fmt = _DETAIL.get(event.event_type)
    text = (
        fmt(event)
        if fmt
        else f"<b>{_esc(event.company_name) if event.company_name else _ticker(event.symbol)}</b>"
    )

    # Position within the current list, for ◀ Prev / Next ▶ flip-through.
    ids = [e.id for e in await _load_kind(session, chat_id, kind)]
    prev_id = next_id = None
    if event_id in ids:
        i = ids.index(event_id)
        if i > 0:
            prev_id = ids[i - 1]
        if i < len(ids) - 1:
            next_id = ids[i + 1]

    flags = await EventFlagsRepository(session).get(event_id, chat_id)
    return BotAction(
        text=text,
        reply_markup=detail_kb(
            kind,
            source_url=event.source_url,
            prev_id=prev_id,
            next_id=next_id,
            event_id=event.id,
            event_type=event.event_type,
            starred=bool(flags and flags.starred),
        ),
        edit=True,
    )


async def _settings_view(session: AsyncSession) -> BotAction:
    from catalyst_radar.runtime_config import effective

    eff = await effective(session)
    toggles = {k: bool(getattr(eff, k)) for k in TOGGLEABLE}
    mode = "webhook" if settings.telegram_webhook_url else "polling"
    text = (
        "<b>Settings</b>\n"
        f"Delivery: <b>{mode}</b>\n"
        f"IPO countries: <code>{_esc(eff.eodhd_ipo_enabled_countries)}</code>\n"
        f"Industry filter: <code>{_esc(eff.ipo_industry_keywords or 'none')}</code>\n"
        f"Min deal size: <code>{_esc(eff.ipo_min_deal_size_usd or 'none')}</code>\n"
        f"Exchange filter: <code>{_esc(eff.ipo_exchange_filter or 'all')}</code>\n"
        f"Timezone: <code>{_esc(eff.alert_timezone)}</code>\n"
        "<i>Tap a toggle to change it. Text filters are web-only.</i>"
    )
    return BotAction(text=text, reply_markup=settings_kb(toggles), edit=True)


def _username(frm: dict[str, Any] | None) -> str | None:
    """The sender's Telegram @username (without @) from a `from` object, or
    None when they have no public username. Used as the listing-day tag."""
    return (frm or {}).get("username") or None


async def handle_command(session: AsyncSession, chat_id: str, text: str) -> BotAction:
    command = text.strip().split()[0].lower().lstrip("/")
    command = command.split("@")[0]  # strip @botname

    if command == "start":
        await TelegramChatRepository(session).register(chat_id, chat_type="private")
        return BotAction(
            text=(
                "<b>Chat registered.</b>\n"
                "<i>You will receive Catalyst Radar alerts here.</i>\n\n" + MENU_TEXT
            ),
            reply_markup=menu_kb(),
            edit=True,
        )

    if command in {"help", ""}:
        return await _help_view()
    if command == "watchlist":
        return await _watchlist_view(session)
    if command == "settings":
        return await _settings_view(session)
    if command == "today":
        return await _kind_view(session, chat_id, "today", 0)
    if command == "thisweek":
        return await _kind_view(session, chat_id, "week", 0)
    if command == "earnings":
        return await _kind_view(session, chat_id, "earn", 0)
    if command == "ipo":
        return await _kind_view(session, chat_id, "ipo", 0)
    if command == "catalysts":
        return await _kind_view(session, chat_id, "cat", 0)
    if command in {"add", "remove"}:
        return BotAction(
            text=(
                "Company metadata entry is web-only for now. "
                "Use the Company Search page in the dashboard."
            ),
            reply_markup=menu_kb(),
            edit=True,
        )

    return BotAction(
        text=f"Unknown command: /{_esc(command)}\n\n{HELP_TEXT}",
        reply_markup=menu_kb(),
        edit=True,
    )


async def _toggle_setting(session: AsyncSession, key: str) -> str:
    """Flip a boolean runtime-config key. Returns a short toast string."""
    from catalyst_radar.runtime_config import effective

    eff = await effective(session)
    new_value = not bool(getattr(eff, key))
    await ConfigRepository(session).set(key, "true" if new_value else "false")
    label = TOGGLEABLE.get(key, key)
    return f"{label}: {'on' if new_value else 'off'}"


def _eid(data: str) -> int | None:
    """Parse the trailing integer event id from a callback token like
    'sr:42'. Returns None for malformed data."""
    part = data.split(":", 1)[1] if ":" in data else ""
    return int(part) if part.isdigit() else None


def _collapsed_text(event: Event) -> str:
    """One-line replacement shown when an IPO alert is dismissed."""
    sym = f"${event.symbol}" if event.symbol else "?"
    when = (
        event.event_date.date().isoformat()
        if event.event_date and hasattr(event.event_date, "date")
        else "TBD"
    )
    return f"🙈 <i>Dismissed</i> — <b>{_esc(sym)}</b> · {when}"


async def _toggle_star(
    session: AsyncSession,
    chat_id: str,
    eid: int | None,
    *,
    surface: str,
    kind: str = "star",
) -> BotAction:
    if eid is None:
        return await _menu_view()
    event = await EventRepository(session).get(eid)
    if event is None:
        action = await _menu_view()
        action.answer_text = "No longer available"
        return action
    repo = EventFlagsRepository(session)
    flags = await repo.get(eid, chat_id)
    new_value = not bool(flags and flags.starred)
    await repo.set_starred(eid, chat_id, new_value)
    toast = (
        "⭐ Starred — you'll get a reminder on listing day"
        if new_value
        else "Star removed"
    )
    if surface == "detail":
        action = await _detail_view(session, chat_id, eid, kind)
    else:
        text, keyboard = render_ipo_alert(event, starred=new_value)
        action = BotAction(text=text or "", reply_markup=keyboard, edit=True)
    action.answer_text = toast
    return action


async def _dismiss(session: AsyncSession, chat_id: str, eid: int | None) -> BotAction:
    if eid is None:
        return await _menu_view()
    event = await EventRepository(session).get(eid)
    if event is None:
        action = await _menu_view()
        action.answer_text = "No longer available"
        return action
    await EventFlagsRepository(session).set_dismissed(eid, chat_id, True)
    return BotAction(
        text=_collapsed_text(event),
        reply_markup=dismissed_kb(eid),
        edit=True,
        answer_text="Dismissed",
    )


async def _undo_dismiss(session: AsyncSession, chat_id: str, eid: int | None) -> BotAction:
    if eid is None:
        return await _menu_view()
    event = await EventRepository(session).get(eid)
    if event is None:
        action = await _menu_view()
        action.answer_text = "No longer available"
        return action
    repo = EventFlagsRepository(session)
    flags = await repo.set_dismissed(eid, chat_id, False)
    text, keyboard = render_ipo_alert(event, starred=bool(flags and flags.starred))
    return BotAction(
        text=text or "", reply_markup=keyboard, edit=True, answer_text="Restored"
    )


async def handle_callback(session: AsyncSession, data: str, chat_id: str) -> BotAction:
    """Route an inline-button tap (callback_data) from ``chat_id`` to a screen."""
    if data == "m":
        return await _menu_view()
    if data == "h":
        return await _help_view()
    if data == "w":
        return await _watchlist_view(session)
    if data == "s":
        return await _settings_view(session)

    if data.startswith("st:"):
        key = data[3:]
        if not _is_admin_chat(chat_id):
            action = await _settings_view(session)
            action.answer_text = "Only admins can change settings"
            return action
        if key in TOGGLEABLE:
            toast = await _toggle_setting(session, key)
            action = await _settings_view(session)
            action.answer_text = toast
            return action
        return await _settings_view(session)

    if data.startswith("v:"):
        _, kind, page = (data.split(":") + ["0"])[:3]
        return await _kind_view(session, chat_id, kind, int(page) if page.isdigit() else 0)

    if data.startswith("d:"):
        parts = data.split(":")
        if len(parts) >= 3 and parts[1].isdigit():
            return await _detail_view(session, chat_id, int(parts[1]), parts[2])
        return await _menu_view()

    if data.startswith("sr:"):
        return await _toggle_star(session, chat_id, _eid(data), surface="alert")
    if data.startswith("sd:"):
        parts = data.split(":")
        eid = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else None
        kind = parts[2] if len(parts) >= 3 else "star"
        return await _toggle_star(session, chat_id, eid, surface="detail", kind=kind)
    if data.startswith("dx:"):
        return await _dismiss(session, chat_id, _eid(data))
    if data.startswith("un:"):
        return await _undo_dismiss(session, chat_id, _eid(data))

    return await _menu_view()


async def _process_callback(session: AsyncSession, cb: dict[str, Any]) -> BotAction | None:
    message = cb.get("message") or {}
    chat_id = str(message.get("chat", {}).get("id", ""))
    if not chat_id or not _may_use_bot(chat_id):
        log.info("telegram_ignored_callback", chat_id=chat_id)
        return None
    action = await handle_callback(session, cb.get("data") or "", chat_id)
    # Taps are the most frequent interaction, so this is where existing chats
    # pick up their @username for the listing-day tag.
    await TelegramChatRepository(session).set_username(
        chat_id, _username(cb.get("from"))
    )
    action.chat_id = chat_id
    action.edit_message_id = message.get("message_id")
    action.callback_query_id = cb.get("id")
    return action


async def process_update(session: AsyncSession, update: dict[str, Any]) -> BotAction | None:
    """Process one Telegram update, deduped by update_id. Returns a
    BotAction the caller delivers, or None when ignored."""
    update_id = update.get("update_id")
    if update_id is None:
        return None

    cfg = ConfigRepository(session)
    last = await cfg.get(_LAST_UPDATE_KEY)
    if last is not None and int(update_id) <= int(last):
        return None  # already processed (also guards webhook retries)
    await cfg.set(_LAST_UPDATE_KEY, int(update_id))

    callback = update.get("callback_query")
    if callback is not None:
        return await _process_callback(session, callback)

    message = update.get("message") or update.get("edited_message")
    if message is None:
        return None
    chat = message.get("chat", {})
    chat_id = str(chat.get("id", ""))
    text = message.get("text", "")
    if not chat_id or not text.startswith("/"):
        return None
    if not _may_use_bot(chat_id):
        log.info("telegram_ignored_chat", chat_id=chat_id)
        if text.strip().lower().startswith("/start"):
            # Private bot: don't register, but tell the owner-to-be which id
            # to add to TELEGRAM_ADMIN_CHAT_ID (first-run setup).
            return BotAction(
                text=(
                    "<b>This Catalyst Radar bot is private.</b>\n"
                    f"Your chat id is <code>{_esc(chat_id)}</code>. If this is your "
                    "deployment, add it to <code>TELEGRAM_ADMIN_CHAT_ID</code>."
                ),
                chat_id=chat_id,
            )
        return None

    action = await handle_command(session, chat_id, text)
    # Capture the sender's @username for the listing-day tag. After /start the
    # chat exists, so this updates the freshly-registered row.
    await TelegramChatRepository(session).set_username(
        chat_id, _username(message.get("from"))
    )
    action.chat_id = chat_id
    return action


async def deliver_action(client: Any, action: BotAction) -> None:
    """Uniform delivery for poller and webhook: acknowledge a tap, then
    edit the source message in place when possible, else send fresh."""
    if action.callback_query_id:
        await client.answer_callback_query(action.callback_query_id, action.answer_text)
    if not action.chat_id or not action.text:
        return
    if action.edit and action.edit_message_id is not None:
        res = await client.edit_message_text(
            action.chat_id,
            action.edit_message_id,
            action.text,
            reply_markup=action.reply_markup,
        )
        if res.ok:
            return  # edited in place; do not also send a new message
    await client.send_message(
        action.chat_id, action.text, reply_markup=action.reply_markup
    )


async def poll_telegram(session: AsyncSession, client: Any | None = None) -> int:
    """One getUpdates poll cycle. Honors telegram_polling_enabled. Returns
    the number of updates that produced a reply. Dedup/offset are handled
    by process_update via app_config."""
    from catalyst_radar.runtime_config import effective
    from catalyst_radar.services.telegram_client import (
        TelegramClient,
        TelegramConfigError,
    )

    # Webhook and getUpdates are mutually exclusive (Telegram 409). When a
    # webhook URL is configured, polling always no-ops regardless of toggle.
    if settings.telegram_webhook_url:
        return 0

    eff = await effective(session)
    if not eff.telegram_polling_enabled:
        return 0

    client = client or TelegramClient()
    try:
        if not client.configured:
            return 0
        last = await ConfigRepository(session).get(_LAST_UPDATE_KEY)
        offset = int(last) + 1 if last is not None else None
        updates = await client.get_updates(offset=offset, poll_timeout=0)
    except TelegramConfigError:
        return 0

    replied = 0
    for update in updates:
        action = await process_update(session, update)
        if action is None:
            continue
        await deliver_action(client, action)
        replied += 1
    if updates:
        log.info("telegram_poll", fetched=len(updates), replied=replied)
    return replied
