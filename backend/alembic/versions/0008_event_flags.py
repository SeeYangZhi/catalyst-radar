"""event flags (star / dismiss / day-of reminder)

Revision ID: 0008_event_flags
Revises: 0007_source_run_summary
Create Date: 2026-06-22 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008_event_flags"
down_revision: str | None = "0007_source_run_summary"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "event_flags",
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("starred", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("dismissed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("starred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("day_of_notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index(op.f("ix_event_flags_starred"), "event_flags", ["starred"])
    op.create_index(op.f("ix_event_flags_dismissed"), "event_flags", ["dismissed"])


def downgrade() -> None:
    op.drop_index(op.f("ix_event_flags_dismissed"), table_name="event_flags")
    op.drop_index(op.f("ix_event_flags_starred"), table_name="event_flags")
    op.drop_table("event_flags")
