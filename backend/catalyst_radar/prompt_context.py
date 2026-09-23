"""Shared LLM-prompt context fragments.

The model has no clock. Without an explicit anchor it resolves "today",
"this morning", "recently", or "the last N days" however it likes — which
is how a 9-day-old CXMT article (released 2026-05-27) got alerted as
breaking "today". Inject :func:`current_date_line` into every system
prompt at CALL TIME so the anchor tracks the real date.

NEVER fold this into a module-level prompt constant: the Celery worker /
beat processes are long-running, and a constant evaluated at import would
freeze the date at deploy time.
"""

from datetime import UTC, datetime


def current_date_line() -> str:
    """One-line current-date anchor to append to an LLM system prompt.

    Day precision in UTC is enough for recency/staleness judgments — the
    pipeline already treats naive timestamps as UTC."""
    today = datetime.now(UTC).date().isoformat()
    return (
        f"\n\nThe current date is {today} (UTC). Resolve every relative date "
        "expression ('today', 'this morning', 'recently', 'the last N days', "
        "'earlier this week') against it. Treat an item as current ONLY if its "
        "own reported date is on or near this date; never describe an event as "
        "happening 'today' or 'this week' unless its reported date actually "
        "matches — an article that says 'today' but is dated earlier is stale "
        "re-reporting, not breaking news."
    )
