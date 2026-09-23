from datetime import date
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.models.base import as_utc, utcnow
from catalyst_radar.models.event import Event
from catalyst_radar.models.event_flags import EventFlags


class EventFlagsRepository:
    """Per-chat event flags. Every method is scoped to one Telegram chat
    except ``due_day_of``, which spans all chats for the reminder job."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, event_id: int, chat_id: str) -> EventFlags | None:
        return await self.session.get(EventFlags, (event_id, chat_id))

    async def _upsert(self, event_id: int, chat_id: str) -> EventFlags:
        row = await self.get(event_id, chat_id)
        if row is None:
            row = EventFlags(event_id=event_id, chat_id=chat_id)
            self.session.add(row)
        return row

    async def set_starred(self, event_id: int, chat_id: str, value: bool) -> EventFlags:
        row = await self._upsert(event_id, chat_id)
        row.starred = value
        row.starred_at = utcnow() if value else None
        self.session.add(row)
        await self.session.commit()
        await self.session.refresh(row)
        return row

    async def set_dismissed(self, event_id: int, chat_id: str, value: bool) -> EventFlags:
        row = await self._upsert(event_id, chat_id)
        row.dismissed = value
        row.dismissed_at = utcnow() if value else None
        self.session.add(row)
        await self.session.commit()
        await self.session.refresh(row)
        return row

    async def mark_day_of_notified(self, event_id: int, chat_id: str) -> None:
        row = await self._upsert(event_id, chat_id)
        row.day_of_notified_at = utcnow()
        self.session.add(row)
        await self.session.commit()

    async def dismissed_ids(self, chat_id: str) -> set[int]:
        result = await self.session.execute(
            select(EventFlags.event_id).where(
                EventFlags.chat_id == chat_id, EventFlags.dismissed.is_(True)
            )
        )
        return set(result.scalars().all())

    async def starred_chat_ids(self, event_id: int) -> set[str]:
        """Chats that starred this event — renders each recipient's own
        ⭐/☆ button state on a fanned-out alert."""
        result = await self.session.execute(
            select(EventFlags.chat_id).where(
                EventFlags.event_id == event_id, EventFlags.starred.is_(True)
            )
        )
        return set(result.scalars().all())

    async def starred_ipo_events(self, chat_id: str) -> list[Event]:
        """This chat's starred IPO events, newest listing first — backs the
        Starred view. Intentionally not filtered by dismiss (star wins)."""
        result = await self.session.execute(
            select(Event)
            .join(EventFlags, EventFlags.event_id == Event.id)
            .where(
                EventFlags.chat_id == chat_id,
                EventFlags.starred.is_(True),
                Event.event_type == "ipo",
            )
            .order_by(Event.event_date.desc())
            .limit(25)
        )
        return list(result.scalars().all())

    async def due_day_of(self, on_date: date, tz_name: str) -> list[tuple[str, Event]]:
        """``(chat_id, event)`` pairs for starred IPOs listing on ``on_date``
        (the alert timezone's local calendar date) whose day-of reminder has
        not yet fired for that chat. Filters by ``starred`` only — a
        dismissed-but-starred IPO still reminds. The starred set is tiny, so
        the local-date comparison is done in Python to stay DB-agnostic
        (SQLite/PG)."""
        result = await self.session.execute(
            select(EventFlags.chat_id, Event)
            .join(EventFlags, EventFlags.event_id == Event.id)
            .where(
                EventFlags.starred.is_(True),
                EventFlags.day_of_notified_at.is_(None),
                Event.event_type == "ipo",
                Event.event_date.is_not(None),
            )
        )
        tz = ZoneInfo(tz_name)
        return [
            (chat_id, e)
            for chat_id, e in result.all()
            if as_utc(e.event_date).astimezone(tz).date() == on_date
        ]
