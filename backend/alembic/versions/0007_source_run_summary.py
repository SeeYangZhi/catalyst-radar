"""source_runs.summary — durable per-run telemetry

Revision ID: 0007_source_run_summary
Revises: 0006_entity_relationships
Create Date: 2026-06-11 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007_source_run_summary"
down_revision: str | None = "0006_entity_relationships"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "source_runs",
        sa.Column("summary", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("source_runs", "summary")
