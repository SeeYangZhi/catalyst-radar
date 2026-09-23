from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.config import settings
from catalyst_radar.models.base import utcnow
from catalyst_radar.models.notification import Notification, TelegramChat


class TelegramChatRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_chat_id(self, chat_id: str) -> TelegramChat | None:
        result = await self.session.execute(
            select(TelegramChat).where(TelegramChat.chat_id == chat_id)
        )
        return result.scalar_one_or_none()

    async def active(self) -> list[TelegramChat]:
        result = await self.session.execute(
            select(TelegramChat).where(TelegramChat.is_active.is_(True))
        )
        return list(result.scalars().all())

    async def recipients(self) -> list[TelegramChat]:
        """Chats that receive alerts, digests and reminders: every active
        chat when ``TELEGRAM_OPEN_SUBSCRIBE`` is on, otherwise only the
        admin chats (a stray active row can never receive anything)."""
        if settings.telegram_open_subscribe:
            return await self.active()
        return await self.admins()

    async def admins(self) -> list[TelegramChat]:
        """Active chats on the ``TELEGRAM_ADMIN_CHAT_ID`` list — the only
        recipients of ops/monitoring alerts. Empty list = every active chat
        (dev convenience)."""
        chats = await self.active()
        admins = settings.telegram_admin_chat_ids
        return [c for c in chats if c.chat_id in admins] if admins else chats

    async def register(
        self,
        chat_id: str,
        *,
        user_id: int | None = None,
        chat_type: str | None = None,
        title: str | None = None,
        username: str | None = None,
    ) -> TelegramChat:
        chat = await self.get_by_chat_id(chat_id)
        if chat is None:
            chat = TelegramChat(
                chat_id=chat_id,
                user_id=user_id,
                chat_type=chat_type,
                title=title,
                username=username,
                is_active=True,
            )
        else:
            chat.is_active = True
            chat.chat_type = chat_type or chat.chat_type
            chat.title = title or chat.title
            chat.username = username or chat.username
        self.session.add(chat)
        await self.session.commit()
        await self.session.refresh(chat)
        return chat

    async def set_username(self, chat_id: str, username: str | None) -> None:
        """Best-effort capture of a chat's Telegram @username from an incoming
        update. No-op when empty, unchanged, or the chat isn't registered yet
        (registration happens on /start). Keeps the mention tag current."""
        if not username:
            return
        chat = await self.get_by_chat_id(chat_id)
        if chat is None or chat.username == username:
            return
        chat.username = username
        self.session.add(chat)
        await self.session.commit()


class NotificationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_dedup_key(self, dedup_key: str) -> Notification | None:
        result = await self.session.execute(
            select(Notification).where(Notification.dedup_key == dedup_key)
        )
        return result.scalar_one_or_none()

    async def add(self, notification: Notification) -> Notification:
        self.session.add(notification)
        await self.session.commit()
        await self.session.refresh(notification)
        return notification

    async def save(self, notification: Notification) -> Notification:
        self.session.add(notification)
        await self.session.commit()
        await self.session.refresh(notification)
        return notification

    async def get(self, notification_id: int) -> Notification | None:
        return await self.session.get(Notification, notification_id)

    async def list_recent(self, limit: int = 50, status: str | None = None) -> list[Notification]:
        stmt = select(Notification)
        if status:
            stmt = stmt.where(Notification.status == status)
        stmt = stmt.order_by(Notification.created_at.desc()).limit(limit)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_event(self, event_id: int) -> Notification | None:
        """Find the catalyst notification queued/skipped for an event.
        At most one per event (the catalyst dedup_key uses event content_key
        + 'catalyst' suffix, and we only ever queue one alert per catalyst)."""
        result = await self.session.execute(
            select(Notification)
            .where(Notification.event_id == event_id)
            .order_by(Notification.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def pending_event_ids(self) -> set[int]:
        """Event ids that currently have a pending notification — the precise
        'about to be alerted' (imminent) set used by IPO enrichment."""
        result = await self.session.execute(
            select(Notification.event_id).where(
                Notification.status == "pending", Notification.event_id.is_not(None)
            )
        )
        return set(result.scalars().all())

    async def release_dispatch_grace(self, event_ids: list[int]) -> None:
        """Pull pending notifications' dispatch_after to now for the given
        events — used by the enrichers so an event described inline sends in
        the same task instead of waiting out the mint grace."""
        if not event_ids:
            return
        await self.session.execute(
            update(Notification)
            .where(Notification.event_id.in_(event_ids), Notification.status == "pending")
            .values(dispatch_after=utcnow())
        )

    async def get_sent_by_event(self, event_id: int) -> list[Notification]:
        """All editable Telegram messages already sent for an event.

        An IPO mints several notifications (one per reminder window:
        14d/7d/1d), so this returns a list. Rows delivered to at least one
        chat are returned — fully ``sent`` or a partial fan-out still
        ``failed`` for other chats — as long as they carry a
        ``telegram_message_id`` (set on the first successful delivery; the
        per-chat ids live in ``payload["message_ids"]``). Used by the alert-backfill
        path to grow already-sent alerts with a late-arriving description."""
        result = await self.session.execute(
            select(Notification)
            .where(Notification.event_id == event_id)
            .where(Notification.status.in_(["sent", "failed"]))
            .where(Notification.telegram_message_id.is_not(None))
            .order_by(Notification.created_at)
        )
        return list(result.scalars().all())
