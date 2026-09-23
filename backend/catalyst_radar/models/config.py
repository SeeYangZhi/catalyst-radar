from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel

from catalyst_radar.models.base import updated_at_column, utcnow


class AppConfig(SQLModel, table=True):
    """Persistent, editable runtime settings (PRD GET/PUT /settings)."""

    __tablename__ = "app_config"

    id: int | None = Field(default=None, primary_key=True)
    key: str = Field(unique=True, index=True, max_length=128)
    value: Any | None = Field(default=None, sa_column=Column(JSON))
    updated_at: datetime = Field(default_factory=utcnow, sa_column=updated_at_column())
