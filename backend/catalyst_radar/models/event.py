from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel

from catalyst_radar.models.base import (
    created_at_column,
    tz_datetime_column,
    updated_at_column,
    utcnow,
)


class Event(SQLModel, table=True):
    __tablename__ = "events"

    id: int | None = Field(default=None, primary_key=True)
    event_type: str = Field(index=True, max_length=32)  # earnings|ipo|catalyst
    source_name: str = Field(index=True, max_length=64)
    source_event_id: str = Field(unique=True, index=True, max_length=512)
    dedup_key: str = Field(unique=True, index=True, max_length=512)

    company_reference_id: int | None = Field(
        default=None, foreign_key="company_reference.id", index=True
    )
    symbol: str | None = Field(default=None, index=True, max_length=64)
    exchange: str | None = Field(default=None, max_length=32)
    country: str | None = Field(default=None, max_length=8)
    company_name: str | None = Field(default=None, max_length=512)

    title: str | None = Field(default=None, max_length=1024)
    event_date: datetime | None = Field(
        default=None, sa_column=tz_datetime_column(nullable=True, index=True)
    )
    source_url: str | None = Field(default=None, max_length=1024)
    status: str = Field(default="new", max_length=32)  # new|relevant|ignored|notified|listed
    payload: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))

    created_at: datetime = Field(default_factory=utcnow, sa_column=created_at_column())
    updated_at: datetime = Field(default_factory=utcnow, sa_column=updated_at_column())


class EventRelevance(SQLModel, table=True):
    __tablename__ = "event_relevance"

    id: int | None = Field(default=None, primary_key=True)
    event_id: int = Field(foreign_key="events.id", index=True)
    tracked_company_id: int | None = Field(
        default=None, foreign_key="tracked_companies.id", index=True
    )
    matched: bool = Field(default=False, index=True)
    reason: str | None = Field(default=None, max_length=512)
    score: float | None = Field(default=None)
    created_at: datetime = Field(default_factory=utcnow, sa_column=created_at_column())
