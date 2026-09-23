"""telegram_chats: display_name -> username

Revision ID: 0010_chat_username
Revises: 0009_telegram_chat_display_name
Create Date: 2026-06-24 02:00:00.000000

NOTE: revision ids must stay <= 32 chars — Alembic's alembic_version.version_num
is VARCHAR(32). The original id (0010_drop_telegram_chat_display_name, 36 chars)
overflowed it and the upgrade failed at the version bookkeeping step.

The per-chat display-name mention (0009) is replaced by the chat's Telegram
@username, rendered as a literal `@handle` tag on listing-day reminders so
each recipient sees a real mention of themselves. Drop the name column and
add a username column (re-captured from incoming updates).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010_chat_username"
down_revision: str | None = "0009_telegram_chat_display_name"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("telegram_chats", "display_name")
    op.add_column(
        "telegram_chats",
        sa.Column("username", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("telegram_chats", "username")
    op.add_column(
        "telegram_chats",
        sa.Column("display_name", sa.String(length=255), nullable=True),
    )
