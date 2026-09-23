from datetime import datetime

from sqlmodel import Field, SQLModel

from catalyst_radar.models.base import (
    created_at_column,
    tz_datetime_column,
    updated_at_column,
    utcnow,
)


class EventFlags(SQLModel, table=True):
    """Per-chat, per-event UI state: starred (→ listing-day reminder to that
    chat), dismissed (→ collapsed + hidden from that chat's list views), and
    the once-per-chat day-of reminder marker. Every Telegram subscriber has
    independent flags. Kept separate from events.payload, which the
    enrichment pipeline rewrites."""

    __tablename__ = "event_flags"

    event_id: int = Field(foreign_key="events.id", primary_key=True)
    chat_id: str = Field(primary_key=True, index=True)
    starred: bool = Field(default=False, index=True)
    dismissed: bool = Field(default=False, index=True)
    starred_at: datetime | None = Field(
        default=None, sa_column=tz_datetime_column(nullable=True)
    )
    dismissed_at: datetime | None = Field(
        default=None, sa_column=tz_datetime_column(nullable=True)
    )
    day_of_notified_at: datetime | None = Field(
        default=None, sa_column=tz_datetime_column(nullable=True)
    )
    created_at: datetime = Field(default_factory=utcnow, sa_column=created_at_column())
    updated_at: datetime = Field(default_factory=utcnow, sa_column=updated_at_column())
