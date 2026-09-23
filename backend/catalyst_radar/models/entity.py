"""Entity layer: groups cross-listings under one issuer + models the
relationship graph (parent/JV/major-shareholder) that drives catalyst
spillover. A single news event can affect multiple tracked listings; the
``EventAffectedCompany`` join captures that fan-out with the LLM's
per-listing reason + importance verdict."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, Column, Text, UniqueConstraint
from sqlmodel import Field, SQLModel

from catalyst_radar.models.base import (
    created_at_column,
    tz_datetime_column,
    updated_at_column,
    utcnow,
)


class CompanyEntity(SQLModel, table=True):
    """A logical issuer that owns one or more listings. Foxconn entity →
    [2317.TW, ADR, ...]. Created via preflight or 1:1 backfill; users
    merge cross-listings under one entity via the settings page."""

    __tablename__ = "company_entities"

    id: int | None = Field(default=None, primary_key=True)
    canonical_name: str = Field(index=True, max_length=512)
    country: str | None = Field(default=None, index=True, max_length=8)
    summary: str | None = Field(default=None, sa_column=Column(Text))
    source: str = Field(default="manual", max_length=64)
    source_payload: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utcnow, sa_column=created_at_column())
    updated_at: datetime = Field(default_factory=utcnow, sa_column=updated_at_column())


class EntityRelationship(SQLModel, table=True):
    """Directed edge between two entities. ``kind=parent_of`` means
    ``from`` is the parent of ``to``; downward propagation only.
    ``kind=joint_venture`` is traversed bidirectionally at propagation
    time. ``kind=major_shareholder`` means ``from`` holds a stake in
    ``to``; downward only."""

    __tablename__ = "entity_relationships"
    __table_args__ = (
        UniqueConstraint(
            "from_entity_id",
            "to_entity_id",
            "kind",
            name="uq_entity_relationships_from_to_kind",
        ),
        CheckConstraint(
            "from_entity_id <> to_entity_id",
            name="ck_entity_relationships_no_self_loop",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    from_entity_id: int = Field(foreign_key="company_entities.id", index=True)
    to_entity_id: int = Field(foreign_key="company_entities.id", index=True)
    # parent_of | joint_venture | major_shareholder
    kind: str = Field(index=True, max_length=32)
    notes: str | None = Field(default=None, sa_column=Column(Text))
    source: str = Field(default="manual", max_length=64)
    source_payload: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utcnow, sa_column=created_at_column())


class EventAffectedCompany(SQLModel, table=True):
    """One row per (event, tracked_company) that the LLM marked as
    materially impacted. Composite PK gives natural dedup across multi-
    path propagation. ``role`` describes the relationship-kind label
    shown on the Telegram card; ``reason`` is the LLM-generated
    one-liner used as attribution text."""

    __tablename__ = "event_affected_companies"

    event_id: int = Field(foreign_key="events.id", primary_key=True, index=True)
    tracked_company_id: int = Field(
        foreign_key="tracked_companies.id", primary_key=True, index=True
    )
    # primary | subsidiary | parent | sibling | jv_partner | shareholder_of
    role: str = Field(index=True, max_length=32)
    # high | medium | low
    effective_importance: str = Field(max_length=16)
    reason: str | None = Field(default=None, sa_column=Column(Text))
    hop_distance: int = Field(default=0)
    created_at: datetime = Field(default_factory=utcnow, sa_column=created_at_column())


class RelationshipSuggestion(SQLModel, table=True):
    """Inbox for preflight + backfill discoveries. The user reviews
    pending rows on the settings page and accepts/rejects each. Accepted
    rows become ``entity_relationships`` (and may auto-create related
    entities/listings as ``is_parent_only=True``)."""

    __tablename__ = "relationship_suggestions"

    id: int | None = Field(default=None, primary_key=True)
    # preflight | backfill
    source: str = Field(max_length=32)
    tracked_company_id: int | None = Field(
        default=None, foreign_key="tracked_companies.id", index=True
    )
    payload: dict[str, Any] = Field(sa_column=Column(JSON, nullable=False))
    # pending | accepted | rejected
    status: str = Field(default="pending", index=True, max_length=16)
    decided_by_user_id: int | None = Field(default=None, foreign_key="users.id")
    decided_at: datetime | None = Field(
        default=None, sa_column=tz_datetime_column(nullable=True)
    )
    notes: str | None = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(default_factory=utcnow, sa_column=created_at_column())
