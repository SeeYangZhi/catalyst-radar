"""event_flags: per-chat star / dismiss / day-of state

Revision ID: 0011_event_flags_per_chat
Revises: 0010_chat_username
Create Date: 2026-09-23 00:00:00.000000

Flags were global (keyed on event_id only), so every Telegram subscriber
shared one star/dismiss state. Key them on (event_id, chat_id). Existing
rows can't be attributed to whoever tapped them, so they are assigned to
the first chat in ``TELEGRAM_ADMIN_CHAT_ID`` (or dropped when none is
configured — they are only UI state).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0011_event_flags_per_chat"
down_revision: str | None = "0010_chat_username"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None



def _owner_chat_id() -> str | None:
    from catalyst_radar.config import settings

    ids = [i.strip() for i in settings.telegram_admin_chat_id.split(",") if i.strip()]
    return ids[0] if ids else None


def upgrade() -> None:
    op.add_column("event_flags", sa.Column("chat_id", sa.String(), nullable=True))
    owner = _owner_chat_id()
    if owner:
        op.execute(sa.text("UPDATE event_flags SET chat_id = :c").bindparams(c=owner))
    else:
        op.execute("DELETE FROM event_flags")
    op.alter_column("event_flags", "chat_id", nullable=False)
    op.drop_constraint("event_flags_pkey", "event_flags", type_="primary")
    op.create_primary_key("event_flags_pkey", "event_flags", ["event_id", "chat_id"])
    op.create_index(op.f("ix_event_flags_chat_id"), "event_flags", ["chat_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_event_flags_chat_id"), table_name="event_flags")
    op.drop_constraint("event_flags_pkey", "event_flags", type_="primary")
    # Collapse back to one row per event, preferring the owner's flags.
    owner = _owner_chat_id()
    if owner:
        op.execute(
            sa.text(
                "DELETE FROM event_flags WHERE chat_id <> :c AND event_id IN "
                "(SELECT event_id FROM event_flags WHERE chat_id = :c)"
            ).bindparams(c=owner)
        )
    op.execute(
        "DELETE FROM event_flags a USING event_flags b "
        "WHERE a.event_id = b.event_id AND a.chat_id > b.chat_id"
    )
    op.create_primary_key("event_flags_pkey", "event_flags", ["event_id"])
    op.drop_column("event_flags", "chat_id")
