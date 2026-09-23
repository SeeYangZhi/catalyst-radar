from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.models.config import AppConfig


class ConfigRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def all(self) -> dict[str, Any]:
        result = await self.session.execute(select(AppConfig))
        return {row.key: row.value for row in result.scalars().all()}

    async def get(self, key: str) -> Any | None:
        result = await self.session.execute(select(AppConfig).where(AppConfig.key == key))
        row = result.scalar_one_or_none()
        return row.value if row else None

    async def _stage(self, key: str, value: Any) -> None:
        """Upsert a row in the session without committing."""
        result = await self.session.execute(select(AppConfig).where(AppConfig.key == key))
        row = result.scalar_one_or_none()
        if row is None:
            self.session.add(AppConfig(key=key, value=value))
        else:
            row.value = value
            self.session.add(row)

    async def set(self, key: str, value: Any) -> None:
        await self._stage(key, value)
        await self.session.commit()

    async def set_many(self, values: dict[str, Any]) -> None:
        """Upsert several keys with a single commit — all-or-nothing, so a
        DB error mid-way never leaves a partially applied batch."""
        for key, value in values.items():
            await self._stage(key, value)
        await self.session.commit()
