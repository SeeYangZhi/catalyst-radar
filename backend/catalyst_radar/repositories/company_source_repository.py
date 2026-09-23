from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.dedup import normalize_url
from catalyst_radar.models.base import utcnow
from catalyst_radar.models.company import CompanySource

# A source is auto-disabled after this many consecutive failed fetches.
MAX_CONSECUTIVE_FAILURES = 3


class CompanySourceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, source_id: int) -> CompanySource | None:
        return await self.session.get(CompanySource, source_id)

    async def get_by_company_url(
        self, tracked_company_id: int, url: str
    ) -> CompanySource | None:
        result = await self.session.execute(
            select(CompanySource).where(
                CompanySource.tracked_company_id == tracked_company_id,
                CompanySource.url == url,
            )
        )
        return result.scalar_one_or_none()

    async def list_for_company(
        self, tracked_company_id: int, *, include_inactive: bool = False
    ) -> list[CompanySource]:
        stmt = select(CompanySource).where(
            CompanySource.tracked_company_id == tracked_company_id
        )
        if not include_inactive:
            stmt = stmt.where(CompanySource.is_active.is_(True))
        stmt = stmt.order_by(CompanySource.kind, CompanySource.id)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_crawlable(self) -> list[CompanySource]:
        """Active sources eligible for the Phase B crawler (not broken/disabled)."""
        result = await self.session.execute(
            select(CompanySource).where(
                CompanySource.is_active.is_(True),
                CompanySource.status == "active",
            )
        )
        return list(result.scalars().all())

    async def create(
        self,
        *,
        tracked_company_id: int,
        kind: str,
        url: str,
        label: str | None = None,
        fetch_strategy: str = "auto",
        source: str = "manual",
        needs_review: bool = False,
        discovery_payload: dict[str, Any] | None = None,
    ) -> CompanySource:
        record = CompanySource(
            tracked_company_id=tracked_company_id,
            kind=kind,
            url=url,
            label=label,
            fetch_strategy=fetch_strategy,
            source=source,
            needs_review=needs_review,
            discovery_payload=discovery_payload,
        )
        self.session.add(record)
        await self.session.commit()
        await self.session.refresh(record)
        return record

    async def upsert_discovered(
        self,
        *,
        tracked_company_id: int,
        kind: str,
        url: str,
        label: str | None,
        source: str,
        discovery_payload: dict[str, Any] | None,
        needs_review: bool = True,
    ) -> tuple[CompanySource, bool]:
        """Insert a discovered source, or no-op if the URL is already present.

        Returns (record, created). Existing rows are never overwritten so a
        manual edit or a deliberate disable survives a re-discovery run.
        """
        url = normalize_url(url)  # so re-discovery with a trailing slash is a no-op
        existing = await self.get_by_company_url(tracked_company_id, url)
        if existing is not None:
            return existing, False
        record = CompanySource(
            tracked_company_id=tracked_company_id,
            kind=kind,
            url=url,
            label=label,
            source=source,
            needs_review=needs_review,
            discovery_payload=discovery_payload,
            last_verified_at=utcnow(),
        )
        self.session.add(record)
        await self.session.flush()
        return record, True

    async def update(self, source_id: int, **fields: Any) -> CompanySource | None:
        record = await self.get(source_id)
        if record is None:
            return None
        for key, value in fields.items():
            setattr(record, key, value)
        self.session.add(record)
        await self.session.commit()
        await self.session.refresh(record)
        return record

    async def delete(self, source_id: int) -> bool:
        record = await self.get(source_id)
        if record is None:
            return False
        await self.session.delete(record)
        await self.session.commit()
        return True

    async def record_fetch_success(
        self,
        source_id: int,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        had_new_item: bool = False,
    ) -> None:
        record = await self.get(source_id)
        if record is None:
            return
        now = utcnow()
        record.last_fetched_at = now
        record.consecutive_failures = 0
        record.last_error = None
        if record.status == "broken":
            record.status = "active"
        if etag is not None:
            record.etag = etag
        if last_modified is not None:
            record.last_modified = last_modified
        if had_new_item:
            record.last_item_at = now
        self.session.add(record)
        await self.session.flush()

    async def record_fetch_failure(self, source_id: int, error: str) -> None:
        record = await self.get(source_id)
        if record is None:
            return
        record.last_fetched_at = utcnow()
        record.consecutive_failures += 1
        record.last_error = error[:1024]
        if record.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            record.status = "broken"
        self.session.add(record)
        await self.session.flush()
