from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Column, Text
from sqlmodel import Field, SQLModel

from catalyst_radar.models.base import created_at_column, utcnow


class ClassifierRun(SQLModel, table=True):
    __tablename__ = "classifier_runs"

    id: int | None = Field(default=None, primary_key=True)
    raw_item_id: int | None = Field(default=None, foreign_key="raw_items.id", index=True)
    event_id: int | None = Field(default=None, foreign_key="events.id", index=True)
    model: str = Field(max_length=64)
    prompt_version: str = Field(max_length=32)
    status: str = Field(default="pending", max_length=32)  # pending|completed|failed|incomplete
    response_id: str | None = Field(default=None, max_length=128)
    input_tokens: int | None = Field(default=None)
    output_tokens: int | None = Field(default=None)
    is_company_critical: bool | None = Field(default=None, index=True)
    output: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    error: str | None = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(default_factory=utcnow, sa_column=created_at_column())
