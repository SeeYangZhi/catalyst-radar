from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Column, UniqueConstraint
from sqlmodel import Field, SQLModel

from catalyst_radar.models.base import (
    created_at_column,
    tz_datetime_column,
    updated_at_column,
    utcnow,
)


class CompanyReference(SQLModel, table=True):
    __tablename__ = "company_reference"
    __table_args__ = (
        UniqueConstraint("exchange", "symbol", name="uq_company_reference_exchange_symbol"),
    )

    id: int | None = Field(default=None, primary_key=True)
    symbol: str = Field(index=True, max_length=64)
    exchange: str = Field(index=True, max_length=32)
    country: str | None = Field(default=None, index=True, max_length=8)
    company_name: str = Field(index=True, max_length=512)
    sector: str | None = Field(default=None, max_length=128)
    industry: str | None = Field(default=None, max_length=128)
    currency: str | None = Field(default=None, max_length=8)
    isin: str | None = Field(default=None, max_length=32)
    corp_code: str | None = Field(default=None, max_length=32)  # OpenDART mapping
    aliases: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    source: str = Field(default="eodhd", max_length=64)
    source_payload: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    is_active: bool = Field(default=True)
    last_seen_at: datetime | None = Field(default=None, sa_column=tz_datetime_column(nullable=True))
    created_at: datetime = Field(default_factory=utcnow, sa_column=created_at_column())
    updated_at: datetime = Field(default_factory=utcnow, sa_column=updated_at_column())


class TrackedCompany(SQLModel, table=True):
    __tablename__ = "tracked_companies"
    __table_args__ = (
        UniqueConstraint("exchange", "symbol", name="uq_tracked_companies_exchange_symbol"),
    )

    id: int | None = Field(default=None, primary_key=True)
    company_reference_id: int | None = Field(
        default=None, foreign_key="company_reference.id", index=True
    )
    # Every tracked listing belongs to an entity (issuer). Entities group
    # cross-listings (Hon Hai TW + ADR) and anchor the relationship graph
    # used for catalyst spillover propagation. Typed Optional so legacy
    # test paths that create a TrackedCompany without first seeding an
    # entity still work; the DB-side migration enforces NOT NULL on real
    # databases (every existing row got a 1:1 entity_id backfilled).
    entity_id: int | None = Field(
        default=None, foreign_key="company_entities.id", index=True
    )
    symbol: str = Field(index=True, max_length=64)
    exchange: str = Field(index=True, max_length=32)
    country: str | None = Field(default=None, max_length=8)
    company_name: str = Field(max_length=512)
    sector: str | None = Field(default=None, max_length=128)
    themes: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    aliases: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    source: str = Field(default="manual", max_length=64)
    source_payload: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    # Set when an entity was auto-tracked solely so its news classifies
    # for downstream propagation (e.g. Foxconn auto-added because user
    # tracks Shunsin). Solo events on parent_only listings drop silently
    # unless they affect a non-parent-only tracked listing.
    is_parent_only: bool = Field(default=False, index=True)
    is_active: bool = Field(default=True, index=True)
    created_at: datetime = Field(default_factory=utcnow, sa_column=created_at_column())
    updated_at: datetime = Field(default_factory=utcnow, sa_column=updated_at_column())


class CompanySource(SQLModel, table=True):
    """A primary-source feed/page for a tracked company (IR press, blog, RSS).

    Populated by the discovery agent (``source="discovery_*"``) or by hand
    (``source="manual"``). The crawler (Phase B) reads ``url`` per cycle and
    uses the ETag/Last-Modified bookkeeping for cheap conditional GETs.
    """

    __tablename__ = "company_sources"
    __table_args__ = (
        UniqueConstraint("tracked_company_id", "url", name="uq_company_sources_company_url"),
    )

    id: int | None = Field(default=None, primary_key=True)
    tracked_company_id: int = Field(foreign_key="tracked_companies.id", index=True)
    kind: str = Field(index=True, max_length=32)  # rss | ir_press | blog | sec | hkex | twitter
    url: str = Field(max_length=2048)
    label: str | None = Field(default=None, max_length=256)

    # active | broken | structure_changed | disabled
    status: str = Field(default="active", index=True, max_length=32)
    # auto | static | browser | agent
    fetch_strategy: str = Field(default="auto", max_length=32)

    # Crawler bookkeeping (Phase B).
    etag: str | None = Field(default=None, max_length=512)
    last_modified: str | None = Field(default=None, max_length=128)
    last_verified_at: datetime | None = Field(
        default=None, sa_column=tz_datetime_column(nullable=True)
    )
    last_fetched_at: datetime | None = Field(
        default=None, sa_column=tz_datetime_column(nullable=True)
    )
    last_item_at: datetime | None = Field(
        default=None, sa_column=tz_datetime_column(nullable=True)
    )
    consecutive_failures: int = Field(default=0)
    last_error: str | None = Field(default=None, max_length=1024)

    # Provenance (Phase D). manual | discovery_openai | discovery_claude
    source: str = Field(default="manual", max_length=64)
    needs_review: bool = Field(default=False, index=True)
    discovery_payload: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))

    is_active: bool = Field(default=True, index=True)
    created_at: datetime = Field(default_factory=utcnow, sa_column=created_at_column())
    updated_at: datetime = Field(default_factory=utcnow, sa_column=updated_at_column())
