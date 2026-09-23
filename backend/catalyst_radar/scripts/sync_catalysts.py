import asyncio

from catalyst_radar.db import async_session_factory
from catalyst_radar.logging import get_logger
from catalyst_radar.services.catalyst_sync import sync_catalysts
from catalyst_radar.services.dispatch import deliver_pending_notifications

log = get_logger("sync_catalysts")


async def _main() -> None:
    async with async_session_factory() as session:
        s = await sync_catalysts(session)
        d = await deliver_pending_notifications(session)
    log.info(
        "sync_catalysts_done",
        fetched=s.fetched,
        classified=s.classified,
        prefiltered=s.prefiltered,
        catalysts=s.catalysts,
        autosent=s.autosent,
        review=s.review,
        skipped=s.skipped,
        errors=s.errors,
        delivered=d.sent,
        delivery_failed=d.failed,
    )


if __name__ == "__main__":
    asyncio.run(_main())
