"""entity relationships and LLM-assessed propagation

Adds the entity layer that groups listings together so cross-listed issuers
(e.g. Hon Hai = 2317.TW + ADR + ...) are a single thing as far as
catalyst propagation is concerned. Tracked listings FK to an entity;
relationships live entity↔entity. A new ``event_affected_companies`` join
table captures which tracked listings a single catalyst event materially
impacts (LLM-assessed), with per-row role + importance + reason. A
``relationship_suggestions`` queue holds preflight/backfill candidates
until the user reviews them.

Backfills 1:1 entities for every existing tracked_company so the schema
flips to NOT NULL without orphans.

Revision ID: 0006_entity_relationships
Revises: 0005_notification_dispatch_after
Create Date: 2026-05-26 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel

from alembic import op

revision: str = "0006_entity_relationships"
down_revision: str | None = "0005_notification_dispatch_after"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "company_entities",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "canonical_name", sqlmodel.sql.sqltypes.AutoString(length=512), nullable=False
        ),
        sa.Column("country", sqlmodel.sql.sqltypes.AutoString(length=8), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column(
            "source",
            sqlmodel.sql.sqltypes.AutoString(length=64),
            nullable=False,
            server_default="manual",
        ),
        sa.Column("source_payload", sa.JSON(), nullable=True),
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
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_company_entities_canonical_name"),
        "company_entities",
        ["canonical_name"],
        unique=False,
    )
    op.create_index(
        op.f("ix_company_entities_country"), "company_entities", ["country"], unique=False
    )

    op.add_column(
        "tracked_companies",
        sa.Column("entity_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "tracked_companies",
        sa.Column(
            "is_parent_only",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    # Backfill: one entity per existing tracked_company so the FK flips
    # to NOT NULL without orphans. We don't try to merge cross-listings
    # automatically — the user does that via the settings page.
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    rows = bind.execute(
        sa.text("SELECT id, company_name, country FROM tracked_companies")
    ).fetchall()
    for tc_id, name, country in rows:
        if is_pg:
            entity_id = bind.execute(
                sa.text(
                    "INSERT INTO company_entities (canonical_name, country, source) "
                    "VALUES (:n, :c, 'backfill_1to1') RETURNING id"
                ),
                {"n": name, "c": country},
            ).scalar_one()
        else:
            # SQLite doesn't support RETURNING on older builds (3.35+ does,
            # but dev databases may be older). Use the dialect-portable
            # two-step instead: INSERT then read lastrowid.
            result = bind.execute(
                sa.text(
                    "INSERT INTO company_entities (canonical_name, country, source) "
                    "VALUES (:n, :c, 'backfill_1to1')"
                ),
                {"n": name, "c": country},
            )
            entity_id = result.lastrowid
        bind.execute(
            sa.text("UPDATE tracked_companies SET entity_id = :e WHERE id = :i"),
            {"e": entity_id, "i": tc_id},
        )

    op.alter_column("tracked_companies", "entity_id", nullable=False)
    op.create_foreign_key(
        "fk_tracked_companies_entity_id",
        "tracked_companies",
        "company_entities",
        ["entity_id"],
        ["id"],
    )
    op.create_index(
        op.f("ix_tracked_companies_entity_id"),
        "tracked_companies",
        ["entity_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_tracked_companies_is_parent_only"),
        "tracked_companies",
        ["is_parent_only"],
        unique=False,
    )

    op.create_table(
        "entity_relationships",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("from_entity_id", sa.Integer(), nullable=False),
        sa.Column("to_entity_id", sa.Integer(), nullable=False),
        sa.Column("kind", sqlmodel.sql.sqltypes.AutoString(length=32), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "source",
            sqlmodel.sql.sqltypes.AutoString(length=64),
            nullable=False,
            server_default="manual",
        ),
        sa.Column("source_payload", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["from_entity_id"], ["company_entities.id"]),
        sa.ForeignKeyConstraint(["to_entity_id"], ["company_entities.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "from_entity_id",
            "to_entity_id",
            "kind",
            name="uq_entity_relationships_from_to_kind",
        ),
        sa.CheckConstraint(
            "from_entity_id <> to_entity_id",
            name="ck_entity_relationships_no_self_loop",
        ),
    )
    op.create_index(
        op.f("ix_entity_relationships_from_entity_id"),
        "entity_relationships",
        ["from_entity_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_entity_relationships_to_entity_id"),
        "entity_relationships",
        ["to_entity_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_entity_relationships_kind"),
        "entity_relationships",
        ["kind"],
        unique=False,
    )

    op.create_table(
        "event_affected_companies",
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("tracked_company_id", sa.Integer(), nullable=False),
        sa.Column("role", sqlmodel.sql.sqltypes.AutoString(length=32), nullable=False),
        sa.Column(
            "effective_importance",
            sqlmodel.sql.sqltypes.AutoString(length=16),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("hop_distance", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tracked_company_id"], ["tracked_companies.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("event_id", "tracked_company_id"),
    )
    op.create_index(
        op.f("ix_event_affected_companies_tracked_company_id"),
        "event_affected_companies",
        ["tracked_company_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_event_affected_companies_role"),
        "event_affected_companies",
        ["role"],
        unique=False,
    )

    op.create_table(
        "relationship_suggestions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source", sqlmodel.sql.sqltypes.AutoString(length=32), nullable=False),
        sa.Column("tracked_company_id", sa.Integer(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            sqlmodel.sql.sqltypes.AutoString(length=16),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("decided_by_user_id", sa.Integer(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tracked_company_id"], ["tracked_companies.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["decided_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_relationship_suggestions_status"),
        "relationship_suggestions",
        ["status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_relationship_suggestions_tracked_company_id"),
        "relationship_suggestions",
        ["tracked_company_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_relationship_suggestions_tracked_company_id"),
        table_name="relationship_suggestions",
    )
    op.drop_index(
        op.f("ix_relationship_suggestions_status"), table_name="relationship_suggestions"
    )
    op.drop_table("relationship_suggestions")

    op.drop_index(
        op.f("ix_event_affected_companies_role"), table_name="event_affected_companies"
    )
    op.drop_index(
        op.f("ix_event_affected_companies_tracked_company_id"),
        table_name="event_affected_companies",
    )
    op.drop_table("event_affected_companies")

    op.drop_index(
        op.f("ix_entity_relationships_kind"), table_name="entity_relationships"
    )
    op.drop_index(
        op.f("ix_entity_relationships_to_entity_id"), table_name="entity_relationships"
    )
    op.drop_index(
        op.f("ix_entity_relationships_from_entity_id"), table_name="entity_relationships"
    )
    op.drop_table("entity_relationships")

    op.drop_index(
        op.f("ix_tracked_companies_is_parent_only"), table_name="tracked_companies"
    )
    op.drop_index(op.f("ix_tracked_companies_entity_id"), table_name="tracked_companies")
    op.drop_constraint(
        "fk_tracked_companies_entity_id", "tracked_companies", type_="foreignkey"
    )
    op.drop_column("tracked_companies", "is_parent_only")
    op.drop_column("tracked_companies", "entity_id")

    op.drop_index(op.f("ix_company_entities_country"), table_name="company_entities")
    op.drop_index(
        op.f("ix_company_entities_canonical_name"), table_name="company_entities"
    )
    op.drop_table("company_entities")
