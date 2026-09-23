"""telegram_chats display_name

Revision ID: 0009_telegram_chat_display_name
Revises: 0008_event_flags
Create Date: 2026-06-24 00:00:00.000000

Stores the Telegram display name (first/last name or @username) captured
from incoming updates, so listing-day reminders can render a real
``tg://user?id=`` mention with a human-readable anchor.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009_telegram_chat_display_name"
down_revision: str | None = "0008_event_flags"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "telegram_chats",
        sa.Column("display_name", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("telegram_chats", "display_name")
