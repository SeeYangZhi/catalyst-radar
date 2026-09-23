from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Column, Text
from sqlmodel import Field, SQLModel

from catalyst_radar.models.base import (
    created_at_column,
    tz_datetime_column,
    utcnow,
)


class SourceRun(SQLModel, table=True):
    __tablename__ = "source_runs"

    id: int | None = Field(default=None, primary_key=True)
    source_name: str = Field(index=True, max_length=64)
    # running|success|failed|empty|skipped (skipped = API key not configured)
    status: str = Field(default="running", max_length=32)
    started_at: datetime = Field(
        default_factory=utcnow, sa_column=tz_datetime_column(nullable=False)
    )
    finished_at: datetime | None = Field(default=None, sa_column=tz_datetime_column(nullable=True))
    item_count: int = Field(default=0)
    error_count: int = Field(default=0)
    last_error: str | None = Field(default=None, sa_column=Column(Text))
    # Per-run structured counters (e.g. {"url_probes": {...}} —).
    # Durable telemetry: container logs are wiped on deploy, this row isn't.
    summary: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utcnow, sa_column=created_at_column())


class RawItem(SQLModel, table=True):
    __tablename__ = "raw_items"

    id: int | None = Field(default=None, primary_key=True)
    source_name: str = Field(index=True, max_length=64)
    schema_name: str = Field(max_length=128)  # e.g. eodhd.exchange_symbol_list.v1
    source_url: str | None = Field(default=None, max_length=1024)
    http_status: int | None = Field(default=None)
    source_event_id: str | None = Field(default=None, index=True, max_length=512)
    dedup_key: str | None = Field(default=None, index=True, max_length=512)
    raw_payload: Any | None = Field(default=None, sa_column=Column(JSON))
    fetched_at: datetime = Field(
        default_factory=utcnow, sa_column=tz_datetime_column(nullable=False)
    )
    source_run_id: int | None = Field(default=None, foreign_key="source_runs.id", index=True)
    created_at: datetime = Field(default_factory=utcnow, sa_column=created_at_column())
