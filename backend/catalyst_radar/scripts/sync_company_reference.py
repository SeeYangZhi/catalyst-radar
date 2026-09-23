import asyncio

from catalyst_radar.db import async_session_factory
from catalyst_radar.logging import get_logger
from catalyst_radar.services.company_sync import sync_company_reference

log = get_logger("sync_company_reference")


async def _main() -> None:
    async with async_session_factory() as session:
        summary = await sync_company_reference(session)
    log.info(
        "sync_company_reference_done",
        exchanges=summary.exchanges,
        upserted=summary.total_upserted,
        errors=summary.errors,
    )


if __name__ == "__main__":
    asyncio.run(_main())
