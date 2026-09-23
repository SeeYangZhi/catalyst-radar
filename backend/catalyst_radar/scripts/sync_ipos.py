import asyncio

from catalyst_radar.db import async_session_factory
from catalyst_radar.logging import get_logger
from catalyst_radar.services.dispatch import deliver_pending_notifications
from catalyst_radar.services.ipo_sync import sync_ipos

log = get_logger("sync_ipos")


async def _main() -> None:
    async with async_session_factory() as session:
        summary = await sync_ipos(session)
        dispatch = await deliver_pending_notifications(session)
    log.info(
        "sync_ipos_done",
        fetched=summary.fetched,
        events_created=summary.events_created,
        matched=summary.matched,
        notifications_created=summary.notifications_created,
        sent=dispatch.sent,
        failed=dispatch.failed,
    )


if __name__ == "__main__":
    asyncio.run(_main())
