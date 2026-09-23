from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Column, Text
from sqlmodel import Field, SQLModel

from catalyst_radar.models.base import (
    created_at_column,
    tz_datetime_column,
    updated_at_column,
    utcnow,
)


class TelegramChat(SQLModel, table=True):
    __tablename__ = "telegram_chats"

    id: int | None = Field(default=None, primary_key=True)
    chat_id: str = Field(unique=True, index=True, max_length=64)
    user_id: int | None = Field(default=None, foreign_key="users.id", index=True)
    chat_type: str | None = Field(default=None, max_length=32)
    title: str | None = Field(default=None, max_length=255)
    # Telegram @username (without the @) captured from incoming updates.
    # Listing-day reminders tag this chat's user with a literal @handle so
    # they see a real mention. Null until the chat next interacts (or has no
    # public username).
    username: str | None = Field(default=None, max_length=64)
    is_active: bool = Field(default=True, index=True)
    registered_at: datetime = Field(
        default_factory=utcnow, sa_column=tz_datetime_column(nullable=False)
    )
    created_at: datetime = Field(default_factory=utcnow, sa_column=created_at_column())
    updated_at: datetime = Field(default_factory=utcnow, sa_column=updated_at_column())


class Notification(SQLModel, table=True):
    __tablename__ = "notifications"

    id: int | None = Field(default=None, primary_key=True)
    event_id: int | None = Field(default=None, foreign_key="events.id", index=True)
    channel: str = Field(default="telegram", max_length=32)
    chat_id: str | None = Field(default=None, max_length=64)
    dedup_key: str = Field(unique=True, index=True, max_length=512)
    reminder_window: str | None = Field(default=None, max_length=32)
    status: str = Field(default="pending", index=True, max_length=32)  # pending|sent|failed|skipped
    skip_reason: str | None = Field(default=None, max_length=512)
    telegram_message_id: int | None = Field(default=None)
    attempts: int = Field(default=0)
    error: str | None = Field(default=None, sa_column=Column(Text))
    payload: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    sent_at: datetime | None = Field(default=None, sa_column=tz_datetime_column(nullable=True))
    # Earliest time the dispatcher may pick this row up. Used to give the
    # UI's optimistic-undo toast a window in which the user can recall a
    # Send before Celery beat fires. NULL = dispatch immediately.
    dispatch_after: datetime | None = Field(
        default=None, sa_column=tz_datetime_column(nullable=True)
    )
    created_at: datetime = Field(default_factory=utcnow, sa_column=created_at_column())
    updated_at: datetime = Field(default_factory=utcnow, sa_column=updated_at_column())
