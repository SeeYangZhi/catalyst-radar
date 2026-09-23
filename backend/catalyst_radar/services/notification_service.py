from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.models.base import utcnow
from catalyst_radar.models.notification import Notification
from catalyst_radar.repositories.notification_repository import (
    NotificationRepository,
)
from catalyst_radar.services.telegram_client import TelegramClient


@dataclass(slots=True)
class DeliveryOutcome:
    status: str  # sent|failed|skipped
    notification_id: int | None
    detail: str | None = None


class NotificationService:
    """Creates and delivers deduped notifications. Delivery is separate
    from generation and is safe to call repeatedly (idempotent by
    dedup_key + reminder window)."""

    def __init__(self, session: AsyncSession, client: TelegramClient | None = None) -> None:
        self.session = session
        self.repo = NotificationRepository(session)
        self.client = client or TelegramClient()

    async def deliver(
        self,
        *,
        chat_id: str,
        text: str,
        dedup_key: str,
        event_id: int | None = None,
        reminder_window: str | None = None,
    ) -> DeliveryOutcome:
        existing = await self.repo.get_by_dedup_key(dedup_key)
        if existing is not None and existing.status == "sent":
            return DeliveryOutcome("skipped", existing.id, "already sent")

        notification = existing or Notification(
            event_id=event_id,
            channel="telegram",
            chat_id=chat_id,
            dedup_key=dedup_key,
            reminder_window=reminder_window,
            status="pending",
            payload={"text": text},
        )
        if existing is None:
            notification = await self.repo.add(notification)

        notification.attempts += 1
        result = await self.client.send_message(chat_id, text)

        if result.ok:
            notification.status = "sent"
            notification.telegram_message_id = result.message_id
            notification.sent_at = utcnow()
            notification.error = None
            await self.repo.save(notification)
            return DeliveryOutcome("sent", notification.id)

        notification.status = "failed"
        notification.error = result.description
        await self.repo.save(notification)
        return DeliveryOutcome("failed", notification.id, result.description)
