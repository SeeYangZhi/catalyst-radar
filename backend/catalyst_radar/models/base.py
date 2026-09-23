from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import Column, DateTime, func


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(dt: datetime | None) -> datetime | None:
    """Treat a naive timestamp (e.g. read back from SQLite) as UTC so window
    math stays DB-agnostic. Pass-through for None and already-aware values."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def now_local(tz_name: str) -> datetime:
    """Current instant in the configured alert timezone (e.g. Asia/Shanghai)."""
    return datetime.now(ZoneInfo(tz_name))


def today_local(tz_name: str) -> date:
    """Today's calendar date in the configured alert timezone.

    Alert windows and the 'today/tomorrow/yesterday' labels must agree with
    the user's wall clock, not UTC — otherwise a listing renders as 'today'
    while their local clock has already rolled to the next day.
    """
    return now_local(tz_name).date()


def created_at_column() -> Column:
    return Column(DateTime(timezone=True), nullable=False, server_default=func.now())


def updated_at_column() -> Column:
    return Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


def tz_datetime_column(*, nullable: bool, index: bool = False) -> Column:
    """Timezone-aware datetime column for Python-set timestamps.

    asyncpg rejects tz-aware values written into TIMESTAMP WITHOUT TIME
    ZONE columns, so every datetime we set from utcnow() must be tz-aware.
    """
    return Column(DateTime(timezone=True), nullable=nullable, index=index)
