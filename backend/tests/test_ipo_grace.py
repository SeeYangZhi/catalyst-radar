from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.models.base import utcnow
from catalyst_radar.models.notification import Notification, TelegramChat
from catalyst_radar.repositories.notification_repository import NotificationRepository
from catalyst_radar.services.dispatch import deliver_pending_notifications
from tests.test_dispatch import RecorderClient


@pytest.mark.asyncio
async def test_notification_in_grace_is_not_dispatched(db_session: AsyncSession) -> None:
    db_session.add(TelegramChat(chat_id="111", is_active=True))
    db_session.add(
        Notification(
            channel="telegram", dedup_key="g1", status="pending",
            payload={"text": "alert"}, dispatch_after=utcnow() + timedelta(minutes=5),
        )
    )
    await db_session.commit()
    out = await deliver_pending_notifications(db_session, client=RecorderClient())
    assert out.sent == 0


@pytest.mark.asyncio
async def test_release_dispatch_grace_makes_it_sendable(db_session: AsyncSession) -> None:
    db_session.add(TelegramChat(chat_id="111", is_active=True))
    n = Notification(
        event_id=42, channel="telegram", dedup_key="g2", status="pending",
        payload={"text": "alert"}, dispatch_after=utcnow() + timedelta(minutes=5),
    )
    db_session.add(n)
    await db_session.commit()

    await NotificationRepository(db_session).release_dispatch_grace([42])
    await db_session.commit()

    out = await deliver_pending_notifications(db_session, client=RecorderClient())
    assert out.sent == 1
