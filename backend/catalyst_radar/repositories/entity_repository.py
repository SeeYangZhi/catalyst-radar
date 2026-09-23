"""Repositories for the entity layer: entities, relationships, and the
preflight/backfill suggestion review queue.

All writes flush but do not commit — callers compose into larger
transactions (e.g. the track_company endpoint creates the entity + the
tracked_company + the suggestion atomically)."""

# Required so `list[...]` annotations resolve to the builtin even when a
# class defines a method literally named ``list`` (which shadows the
# builtin during class-body evaluation). Without this, eager annotation
# evaluation on Python 3.14 raises `TypeError: 'function' object is not
# subscriptable` at import time.
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.models.base import utcnow
from catalyst_radar.models.entity import (
    CompanyEntity,
    EntityRelationship,
    RelationshipSuggestion,
)


class CompanyEntityRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, entity_id: int) -> CompanyEntity | None:
        return await self.session.get(CompanyEntity, entity_id)

    async def get_by_name(self, canonical_name: str) -> CompanyEntity | None:
        result = await self.session.execute(
            select(CompanyEntity).where(CompanyEntity.canonical_name == canonical_name)
        )
        return result.scalar_one_or_none()

    async def list(self) -> list[CompanyEntity]:
        result = await self.session.execute(
            select(CompanyEntity).order_by(CompanyEntity.canonical_name)
        )
        return list(result.scalars().all())

    async def create(
        self,
        *,
        canonical_name: str,
        country: str | None = None,
        summary: str | None = None,
        source: str = "manual",
        source_payload: dict[str, Any] | None = None,
    ) -> CompanyEntity:
        entity = CompanyEntity(
            canonical_name=canonical_name,
            country=country,
            summary=summary,
            source=source,
            source_payload=source_payload,
        )
        self.session.add(entity)
        await self.session.flush()
        return entity

    async def upsert_by_name(
        self,
        *,
        canonical_name: str,
        country: str | None = None,
        summary: str | None = None,
        source: str = "manual",
        source_payload: dict[str, Any] | None = None,
    ) -> CompanyEntity:
        existing = await self.get_by_name(canonical_name)
        if existing is not None:
            # Update only when new values are non-empty — we don't want
            # a preflight rerun to wipe a user-edited summary.
            changed = False
            if country and not existing.country:
                existing.country = country
                changed = True
            if summary and not existing.summary:
                existing.summary = summary
                changed = True
            if changed:
                existing.updated_at = utcnow()
                self.session.add(existing)
                await self.session.flush()
            return existing
        return await self.create(
            canonical_name=canonical_name,
            country=country,
            summary=summary,
            source=source,
            source_payload=source_payload,
        )

    async def update(
        self,
        entity_id: int,
        *,
        canonical_name: str | None = None,
        country: str | None = None,
        summary: str | None = None,
    ) -> CompanyEntity | None:
        entity = await self.get(entity_id)
        if entity is None:
            return None
        if canonical_name is not None:
            entity.canonical_name = canonical_name
        if country is not None:
            entity.country = country
        if summary is not None:
            entity.summary = summary
        entity.updated_at = utcnow()
        self.session.add(entity)
        await self.session.flush()
        return entity


class EntityRelationshipRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list(self) -> list[EntityRelationship]:
        result = await self.session.execute(select(EntityRelationship))
        return list(result.scalars().all())

    async def list_for_entity(self, entity_id: int) -> list[EntityRelationship]:
        result = await self.session.execute(
            select(EntityRelationship).where(
                (EntityRelationship.from_entity_id == entity_id)
                | (EntityRelationship.to_entity_id == entity_id)
            )
        )
        return list(result.scalars().all())

    async def get(self, rel_id: int) -> EntityRelationship | None:
        return await self.session.get(EntityRelationship, rel_id)

    async def get_existing(
        self, *, from_entity_id: int, to_entity_id: int, kind: str
    ) -> EntityRelationship | None:
        result = await self.session.execute(
            select(EntityRelationship).where(
                EntityRelationship.from_entity_id == from_entity_id,
                EntityRelationship.to_entity_id == to_entity_id,
                EntityRelationship.kind == kind,
            )
        )
        return result.scalar_one_or_none()

    async def create(
        self,
        *,
        from_entity_id: int,
        to_entity_id: int,
        kind: str,
        notes: str | None = None,
        source: str = "manual",
        source_payload: dict[str, Any] | None = None,
    ) -> EntityRelationship:
        existing = await self.get_existing(
            from_entity_id=from_entity_id, to_entity_id=to_entity_id, kind=kind
        )
        if existing is not None:
            return existing
        rel = EntityRelationship(
            from_entity_id=from_entity_id,
            to_entity_id=to_entity_id,
            kind=kind,
            notes=notes,
            source=source,
            source_payload=source_payload,
        )
        self.session.add(rel)
        await self.session.flush()
        return rel

    async def delete(self, rel_id: int) -> bool:
        rel = await self.get(rel_id)
        if rel is None:
            return False
        await self.session.delete(rel)
        await self.session.flush()
        return True


class RelationshipSuggestionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, suggestion_id: int) -> RelationshipSuggestion | None:
        return await self.session.get(RelationshipSuggestion, suggestion_id)

    async def list(
        self, *, status: str | None = "pending"
    ) -> list[RelationshipSuggestion]:
        stmt = select(RelationshipSuggestion).order_by(
            RelationshipSuggestion.created_at.desc()
        )
        if status:
            stmt = stmt.where(RelationshipSuggestion.status == status)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def create(
        self,
        *,
        source: str,
        tracked_company_id: int | None,
        payload: dict[str, Any],
        notes: str | None = None,
    ) -> RelationshipSuggestion:
        sug = RelationshipSuggestion(
            source=source,
            tracked_company_id=tracked_company_id,
            payload=payload,
            status="pending",
            notes=notes,
        )
        self.session.add(sug)
        await self.session.flush()
        return sug

    async def mark_decided(
        self,
        suggestion_id: int,
        *,
        status: str,
        decided_by_user_id: int | None = None,
        notes: str | None = None,
    ) -> RelationshipSuggestion | None:
        sug = await self.get(suggestion_id)
        if sug is None:
            return None
        sug.status = status
        sug.decided_by_user_id = decided_by_user_id
        sug.decided_at = utcnow()
        if notes is not None:
            sug.notes = notes
        self.session.add(sug)
        await self.session.flush()
        return sug
