from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.models.base import utcnow
from catalyst_radar.models.company import CompanyReference, TrackedCompany


class CompanyReferenceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, company_id: int) -> CompanyReference | None:
        return await self.session.get(CompanyReference, company_id)

    async def get_by_exchange_symbol(self, exchange: str, symbol: str) -> CompanyReference | None:
        result = await self.session.execute(
            select(CompanyReference).where(
                CompanyReference.exchange == exchange,
                CompanyReference.symbol == symbol,
            )
        )
        return result.scalar_one_or_none()

    async def upsert(
        self,
        *,
        symbol: str,
        exchange: str,
        company_name: str,
        country: str | None = None,
        sector: str | None = None,
        industry: str | None = None,
        currency: str | None = None,
        isin: str | None = None,
        aliases: list[str] | None = None,
        source: str = "eodhd",
        source_payload: dict[str, Any] | None = None,
    ) -> CompanyReference:
        existing = await self.get_by_exchange_symbol(exchange, symbol)
        now = utcnow()
        if existing is None:
            company = CompanyReference(
                symbol=symbol,
                exchange=exchange,
                company_name=company_name,
                country=country,
                sector=sector,
                industry=industry,
                currency=currency,
                isin=isin,
                aliases=aliases or [],
                source=source,
                source_payload=source_payload,
                is_active=True,
                last_seen_at=now,
            )
            self.session.add(company)
            await self.session.flush()
            return company

        existing.company_name = company_name
        existing.country = country or existing.country
        existing.sector = sector or existing.sector
        existing.industry = industry or existing.industry
        existing.currency = currency or existing.currency
        existing.isin = isin or existing.isin
        if aliases:
            existing.aliases = aliases
        if source_payload is not None:
            existing.source_payload = source_payload
        existing.is_active = True
        existing.last_seen_at = now
        self.session.add(existing)
        await self.session.flush()
        return existing

    async def search(
        self,
        q: str | None = None,
        country: str | None = None,
        exchange: str | None = None,
        limit: int = 25,
    ) -> list[CompanyReference]:
        stmt = select(CompanyReference).where(CompanyReference.is_active.is_(True))
        if q:
            like = f"%{q.strip()}%"
            stmt = stmt.where(
                or_(
                    CompanyReference.symbol.ilike(like),
                    CompanyReference.company_name.ilike(like),
                )
            )
        if country:
            stmt = stmt.where(func.lower(CompanyReference.country) == country.lower())
        if exchange:
            stmt = stmt.where(func.lower(CompanyReference.exchange) == exchange.lower())
        stmt = stmt.order_by(CompanyReference.company_name).limit(min(limit, 100))
        result = await self.session.execute(stmt)
        return list(result.scalars().all())


class TrackedCompanyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, tracked_id: int) -> TrackedCompany | None:
        return await self.session.get(TrackedCompany, tracked_id)

    async def get_by_exchange_symbol(self, exchange: str, symbol: str) -> TrackedCompany | None:
        result = await self.session.execute(
            select(TrackedCompany).where(
                TrackedCompany.exchange == exchange,
                TrackedCompany.symbol == symbol,
            )
        )
        return result.scalar_one_or_none()

    async def list_active(self) -> list[TrackedCompany]:
        result = await self.session.execute(
            select(TrackedCompany)
            .where(TrackedCompany.is_active.is_(True))
            .order_by(TrackedCompany.company_name)
        )
        return list(result.scalars().all())

    async def add(self, company: TrackedCompany) -> TrackedCompany:
        self.session.add(company)
        await self.session.commit()
        await self.session.refresh(company)
        return company

    async def deactivate(self, tracked_id: int) -> bool:
        company = await self.get(tracked_id)
        if company is None:
            return False
        company.is_active = False
        company.updated_at = datetime.now().astimezone()
        self.session.add(company)
        await self.session.commit()
        return True

    async def delete(self, tracked_id: int) -> bool:
        company = await self.get(tracked_id)
        if company is None:
            return False
        await self.session.delete(company)
        await self.session.commit()
        return True
