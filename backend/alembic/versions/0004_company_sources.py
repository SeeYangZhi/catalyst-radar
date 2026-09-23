"""company sources

Revision ID: 0004_company_sources
Revises: 0003_timezone_aware_datetimes
Create Date: 2026-05-22 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel

from alembic import op

revision: str = "0004_company_sources"
down_revision: str | None = "0003_timezone_aware_datetimes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "company_sources",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tracked_company_id", sa.Integer(), nullable=False),
        sa.Column("kind", sqlmodel.sql.sqltypes.AutoString(length=32), nullable=False),
        sa.Column("url", sqlmodel.sql.sqltypes.AutoString(length=2048), nullable=False),
        sa.Column("label", sqlmodel.sql.sqltypes.AutoString(length=256), nullable=True),
        sa.Column(
            "status",
            sqlmodel.sql.sqltypes.AutoString(length=32),
            nullable=False,
            server_default="active",
        ),
        sa.Column(
            "fetch_strategy",
            sqlmodel.sql.sqltypes.AutoString(length=32),
            nullable=False,
            server_default="auto",
        ),
        sa.Column("etag", sqlmodel.sql.sqltypes.AutoString(length=512), nullable=True),
        sa.Column("last_modified", sqlmodel.sql.sqltypes.AutoString(length=128), nullable=True),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_item_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sqlmodel.sql.sqltypes.AutoString(length=1024), nullable=True),
        sa.Column(
            "source",
            sqlmodel.sql.sqltypes.AutoString(length=64),
            nullable=False,
            server_default="manual",
        ),
        sa.Column(
            "needs_review",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("discovery_payload", sa.JSON(), nullable=True),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tracked_company_id"], ["tracked_companies.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tracked_company_id", "url", name="uq_company_sources_company_url"
        ),
    )
    op.create_index(
        op.f("ix_company_sources_tracked_company_id"),
        "company_sources",
        ["tracked_company_id"],
        unique=False,
    )
    op.create_index(op.f("ix_company_sources_kind"), "company_sources", ["kind"], unique=False)
    op.create_index(
        op.f("ix_company_sources_status"), "company_sources", ["status"], unique=False
    )
    op.create_index(
        op.f("ix_company_sources_needs_review"),
        "company_sources",
        ["needs_review"],
        unique=False,
    )
    op.create_index(
        op.f("ix_company_sources_is_active"), "company_sources", ["is_active"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_company_sources_is_active"), table_name="company_sources")
    op.drop_index(op.f("ix_company_sources_needs_review"), table_name="company_sources")
    op.drop_index(op.f("ix_company_sources_status"), table_name="company_sources")
    op.drop_index(op.f("ix_company_sources_kind"), table_name="company_sources")
    op.drop_index(op.f("ix_company_sources_tracked_company_id"), table_name="company_sources")
    op.drop_table("company_sources")
