"""Inline-keyboard UX for the Telegram bot.

Callback-data scheme (Telegram caps callback_data at 64 bytes, so tokens
are short and ASCII):

    m                main menu
    v:<kind>:<page>  list view; kind in KINDS; page is 0-based
    d:<eid>:<kind>   detail card for event <eid>; "Back" returns to <kind>
    s                settings view
    st:<key>         toggle the boolean settings key <key>
    w                watchlist
    h                help / menu text

    fb:<eid>:u       mark event <eid> feedback useful
    fb:<eid>:n       mark event <eid> feedback not_useful

    sr:<eid>         toggle star from an alert card (re-renders the alert)
    sd:<eid>:<kind>  toggle star from the in-menu detail card (re-renders detail in <kind> context)
    dx:<eid>         dismiss event <eid> (collapse message + hide from views)
    un:<eid>         undo dismiss on event <eid>

The builders here are pure (no DB/IO): they take already-loaded data and
return Telegram ``reply_markup`` dicts. telegram_bot.py owns the data and
the dispatch.
"""

from dataclasses import dataclass
from typing import Any

# kind -> (button label, screen title). Order drives the menu layout.
KINDS: dict[str, tuple[str, str]] = {
    "today": ("Today", "Today"),
    "week": ("This week", "This week"),
    "earn": ("Earnings", "Upcoming earnings"),
    "ipo": ("IPOs", "Upcoming IPOs"),
    "cat": ("Catalysts", "Recent catalysts"),
}

# Boolean settings safe to flip from chat. Other (text/number) settings
# stay web-only and render read-only in the settings message. Polling is
# intentionally excluded: it is mutually exclusive with webhook delivery
# (and no-ops whenever a webhook URL is configured), so toggling it from
# chat would be a confusing no-op for the operator.
TOGGLEABLE: dict[str, str] = {
    "telegram_alerts_enabled": "Alerts",
    "ipo_exclude_etfs_trusts": "Exclude ETFs/Trusts",
    "sec_edgar_enrich_enabled": "US filing enrich",
    "hkex_prospectus_enrich_enabled": "HK prospectus enrich",
}

PAGE_SIZE = 6


@dataclass(slots=True)
class BotAction:
    """What to render plus how to deliver it. process_update fills the
    delivery context (chat_id / message ids); the caller (poller or
    webhook) performs the send/edit/answer uniformly."""

    text: str
    reply_markup: dict | None = None
    edit: bool = False  # edit the source message instead of sending new
    answer_text: str | None = None  # toast shown on a button tap

    # Delivery context, populated by process_update.
    chat_id: str | None = None
    edit_message_id: int | None = None
    callback_query_id: str | None = None


def _btn(text: str, data: str) -> dict[str, str]:
    return {"text": text, "callback_data": data}


def _url_btn(text: str, url: str) -> dict[str, str]:
    return {"text": text, "url": url}


def _menu_row() -> list[dict[str, str]]:
    return [_btn("☰ Menu", "m")]


def menu_kb() -> dict[str, Any]:
    """Main menu: one button per list view, then utilities."""
    rows: list[list[dict[str, str]]] = []
    items = [(k, label) for k, (label, _) in KINDS.items()]
    for i in range(0, len(items), 2):
        rows.append(
            [_btn(label, f"v:{k}:0") for k, label in items[i : i + 2]]
        )
    rows.append([_btn("⭐ Starred", "v:star:0")])
    rows.append([_btn("Watchlist", "w"), _btn("Settings", "s")])
    rows.append([_btn("Help", "h")])
    return {"inline_keyboard": rows}


def list_kb(
    items: list[tuple[int, str]], kind: str, page: int, total: int
) -> dict[str, Any]:
    """``items`` is the current page's (event_id, button_label) pairs.
    ``total`` is the full result count, for pagination math."""
    rows: list[list[dict[str, str]]] = [
        [_btn(label, f"d:{eid}:{kind}")] for eid, label in items
    ]
    nav: list[dict[str, str]] = []
    if page > 0:
        nav.append(_btn("◀ Prev", f"v:{kind}:{page - 1}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(_btn("Next ▶", f"v:{kind}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append(_menu_row())
    return {"inline_keyboard": rows}


def detail_kb(
    kind: str,
    *,
    source_url: str | None = None,
    prev_id: int | None = None,
    next_id: int | None = None,
    event_id: int | None = None,
    event_type: str | None = None,
    starred: bool = False,
) -> dict[str, Any]:
    """Detail card navigation: optional star toggle (IPO), source link, then
    flip through the list (◀ Prev / Next ▶), then List / Menu."""
    rows: list[list[dict[str, str]]] = []
    if event_type == "ipo" and event_id is not None:
        star_label = "★ Starred ✓" if starred else "⭐ Star"
        rows.append([_btn(star_label, f"sd:{event_id}:{kind}")])
    if source_url:
        rows.append([_url_btn("🔗 Source / prospectus", source_url)])
    nav: list[dict[str, str]] = []
    if prev_id is not None:
        nav.append(_btn("◀ Prev", f"d:{prev_id}:{kind}"))
    if next_id is not None:
        nav.append(_btn("Next ▶", f"d:{next_id}:{kind}"))
    if nav:
        rows.append(nav)
    rows.append([_btn("≣ List", f"v:{kind}:0"), _btn("☰ Menu", "m")])
    return {"inline_keyboard": rows}


def alert_kb(
    *,
    event_id: int | None = None,
    event_type: str | None = None,
    source_url: str | None = None,
    starred: bool = False,
) -> dict[str, Any]:
    """Action row attached to pushed alert cards. IPO alerts get contextual
    Star/Dismiss actions; other event types keep the legacy Source + Menu
    row. Star wording flips with current state."""
    rows: list[list[dict[str, str]]] = []
    if event_type == "ipo" and event_id is not None:
        star_label = "★ Starred ✓" if starred else "⭐ Star"
        rows.append([_btn(star_label, f"sr:{event_id}")])
        action_row: list[dict[str, str]] = []
        if source_url:
            action_row.append(_url_btn("🔗 Source / prospectus", source_url))
        action_row.append(_btn("🙈 Dismiss", f"dx:{event_id}"))
        rows.append(action_row)
        rows.append(_menu_row())
        return {"inline_keyboard": rows}

    if source_url:
        rows.append([_url_btn("🔗 Source / prospectus", source_url)])
    rows.append(_menu_row())
    return {"inline_keyboard": rows}


def dismissed_kb(event_id: int) -> dict[str, Any]:
    """Keyboard for a collapsed (dismissed) alert: a single Undo button."""
    return {"inline_keyboard": [[_btn("↩ Undo", f"un:{event_id}")]]}


def settings_kb(values: dict[str, bool]) -> dict[str, Any]:
    """One toggle button per boolean key, showing its current state.
    Toggles apply immediately, so the exit is an explicit, full-width
    "Done" rather than the terse shared Menu chip — the lack of an
    obvious way out was the top piece of UX feedback."""
    rows: list[list[dict[str, str]]] = []
    for key, label in TOGGLEABLE.items():
        on = bool(values.get(key))
        state = "🟢 on" if on else "⚪ off"
        rows.append([_btn(f"{label}: {state}", f"st:{key}")])
    rows.append([_btn("✓ Done — back to menu", "m")])
    return {"inline_keyboard": rows}
