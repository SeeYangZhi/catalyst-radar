from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, HttpUrl

SourceKind = Literal["rss", "ir_press", "blog", "sec", "hkex", "twitter"]
FetchStrategy = Literal["auto", "static", "browser", "agent"]


class CompanyReferenceOut(BaseModel):
    id: int
    symbol: str
    exchange: str
    country: str | None
    company_name: str
    sector: str | None
    industry: str | None
    currency: str | None
    isin: str | None
    source: str

    model_config = {"from_attributes": True}


class TrackedCompanyOut(BaseModel):
    id: int
    company_reference_id: int | None
    entity_id: int | None = None
    symbol: str
    exchange: str
    country: str | None
    company_name: str
    sector: str | None
    themes: list[str]
    aliases: list[str]
    source: str
    is_parent_only: bool = False
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class TrackCompanyRequest(BaseModel):
    """Track from a reference row, or provide manual fallback fields."""

    company_reference_id: int | None = None
    symbol: str | None = None
    exchange: str | None = None
    country: str | None = None
    company_name: str | None = None
    sector: str | None = None
    themes: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)


class CompanySourceOut(BaseModel):
    id: int
    tracked_company_id: int
    kind: str
    url: str
    label: str | None
    status: str
    fetch_strategy: str
    last_verified_at: datetime | None
    last_fetched_at: datetime | None
    last_item_at: datetime | None
    consecutive_failures: int
    last_error: str | None
    source: str
    needs_review: bool
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class CompanySourceCreate(BaseModel):
    kind: SourceKind
    url: Annotated[HttpUrl, Field()]
    label: str | None = None
    fetch_strategy: FetchStrategy = "auto"


class CompanySourceUpdate(BaseModel):
    """Partial update; only provided fields are changed."""

    kind: SourceKind | None = None
    url: HttpUrl | None = None
    label: str | None = None
    fetch_strategy: FetchStrategy | None = None
    status: Literal["active", "broken", "structure_changed", "disabled"] | None = None
    is_active: bool | None = None
    needs_review: bool | None = None
