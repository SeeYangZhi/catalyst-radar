from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.models.classifier import ClassifierRun


class ClassifierRunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_recent(self, limit: int = 50) -> list[ClassifierRun]:
        result = await self.session.execute(
            select(ClassifierRun).order_by(ClassifierRun.created_at.desc()).limit(limit)
        )
        return list(result.scalars().all())
