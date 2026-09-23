"""notification dispatch_after

Revision ID: 0005_notification_dispatch_after
Revises: 0004_company_sources
Create Date: 2026-05-24 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005_notification_dispatch_after"
down_revision: str | None = "0004_company_sources"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "notifications",
        sa.Column("dispatch_after", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("notifications", "dispatch_after")
