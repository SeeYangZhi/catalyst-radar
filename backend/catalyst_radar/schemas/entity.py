"""Pydantic API schemas for the entity layer + relationship graph +
preflight + suggestion review queue."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

RelationshipKind = Literal["parent_of", "joint_venture", "major_shareholder"]
SuggestionStatus = Literal["pending", "accepted", "rejected"]
SuggestionSource = Literal["preflight", "backfill"]


class CompanyEntityOut(BaseModel):
    id: int
    canonical_name: str
    country: str | None
    summary: str | None
    source: str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class CompanyEntityCreate(BaseModel):
    canonical_name: str
    country: str | None = None
    summary: str | None = None


class CompanyEntityUpdate(BaseModel):
    canonical_name: str | None = None
    country: str | None = None
    summary: str | None = None


class EntityRelationshipOut(BaseModel):
    id: int
    from_entity_id: int
    to_entity_id: int
    kind: RelationshipKind
    notes: str | None
    source: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class EntityRelationshipCreate(BaseModel):
    from_entity_id: int
    to_entity_id: int
    kind: RelationshipKind
    notes: str | None = None


class PreflightRequest(BaseModel):
    """Either a tracked_company_id (re-run preflight for an existing
    tracked listing) OR the symbol/exchange/name triple to preflight a
    company before tracking it."""

    tracked_company_id: int | None = None
    symbol: str | None = None
    exchange: str | None = None
    country: str | None = None
    company_name: str | None = None


class PreflightEntity(BaseModel):
    canonical_name: str
    country: str
    ticker: str
    exchange: str
    summary: str
    confidence: float


class PreflightResponse(BaseModel):
    status: str  # completed | failed | disabled
    entity: PreflightEntity | None = None
    parents: list[PreflightEntity] = Field(default_factory=list)
    joint_venture_partners: list[PreflightEntity] = Field(default_factory=list)
    major_shareholders: list[PreflightEntity] = Field(default_factory=list)
    sources: list[dict[str, str]] = Field(default_factory=list)
    notes: str = ""
    model: str | None = None
    error: str | None = None
    suggestion_id: int | None = None


class RelationshipSuggestionOut(BaseModel):
    id: int
    source: SuggestionSource
    tracked_company_id: int | None
    payload: dict[str, Any]
    status: SuggestionStatus
    decided_at: datetime | None
    notes: str | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class SuggestionDecision(BaseModel):
    """User's accept/reject for a pending suggestion. When accepting, the
    user picks which relationships from the suggestion payload to apply
    (by index). Each accepted entry upserts the referenced entity, links
    via entity_relationships, and (when ``auto_track`` set) auto-tracks
    a primary listing as ``is_parent_only=True``."""

    decision: Literal["accept", "reject"]
    accepted_keys: list[str] = Field(default_factory=list)
    auto_track: bool = True
    notes: str | None = None


class TrackCompanyWithPreflight(BaseModel):
    """Extends TrackCompanyRequest with an optional ``apply_preflight``
    flag — when set, the server runs preflight inside the same request
    and writes a pending suggestion against the new tracked company."""

    company_reference_id: int | None = None
    symbol: str | None = None
    exchange: str | None = None
    country: str | None = None
    company_name: str | None = None
    sector: str | None = None
    themes: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    apply_preflight: bool = True
    entity_id: int | None = None  # link to an existing entity if user picked one
