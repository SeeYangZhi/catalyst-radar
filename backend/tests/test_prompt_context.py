from datetime import UTC, datetime

from catalyst_radar.prompt_context import current_date_line


def test_current_date_line_carries_today_utc():
    """The anchor must contain the real current UTC date so the model can
    resolve 'today' against it (the CXMT 'today' regression)."""
    today = datetime.now(UTC).date().isoformat()
    line = current_date_line()
    assert today in line
    assert "(UTC)" in line


def test_current_date_line_warns_against_stale_today():
    """It must explicitly tell the model not to call stale re-reporting
    'today' — that wording is what kills the 9-day-old-news bug."""
    line = current_date_line().lower()
    assert "today" in line
    assert "stale" in line


def test_current_date_line_is_evaluated_per_call():
    """Must be a function, not a frozen module constant — long-running
    workers would otherwise pin the date at import time."""
    assert callable(current_date_line)
