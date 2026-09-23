from datetime import date, datetime, time
from typing import Any

from sqlalchemy import delete, func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.models.event import Event, EventRelevance
from catalyst_radar.models.notification import Notification


class EventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_source_event_id(self, source_event_id: str) -> Event | None:
        result = await self.session.execute(
            select(Event).where(Event.source_event_id == source_event_id)
        )
        return result.scalar_one_or_none()

    async def get_by_dedup_key(self, dedup_key: str) -> Event | None:
        result = await self.session.execute(select(Event).where(Event.dedup_key == dedup_key))
        return result.scalar_one_or_none()

    async def delete_stale_ignored_catalysts(self, cutoff: datetime) -> int:
        """Retention sweep for step 4: drop ignored catalyst rows old
        enough that the freshness gate would re-drop the article without an
        LLM call anyway. Rows referenced by EventRelevance or Notification
        are never touched (defensive — ignored rows shouldn't have either)."""
        result = await self.session.execute(
            delete(Event).where(
                Event.event_type == "catalyst",
                Event.status == "ignored",
                Event.created_at < cutoff,
                Event.id.not_in(select(EventRelevance.event_id)),
                Event.id.not_in(
                    select(Notification.event_id).where(Notification.event_id.is_not(None))
                ),
            )
        )
        return int(result.rowcount or 0)

    async def list_recent_catalysts_for_company(
        self, symbol: str, exchange: str, limit: int = 100
    ) -> list[Event]:
        """Most-recent non-ignored catalyst events for ONE company (symbol AND
        exchange — a ticker can exist on two exchanges). The dedup-window cutoff
        is applied by the caller in Python so it is correct on both Postgres
        (tz-aware) and SQLite (naive) without relying on SQL datetime semantics."""
        result = await self.session.execute(
            select(Event)
            .where(
                Event.event_type == "catalyst",
                Event.symbol == symbol,
                Event.exchange == exchange,
                Event.status != "ignored",
            )
            .order_by(Event.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def append_also_seen_in(self, event: Event, entry: dict[str, Any]) -> None:
        payload = dict(event.payload or {})
        seen = list(payload.get("also_seen_in", []))
        seen.append(entry)
        payload["also_seen_in"] = seen
        event.payload = payload
        self.session.add(event)
        await self.session.flush()

    async def upsert(self, event: Event) -> tuple[Event, bool]:
        """Idempotent insert keyed by source_event_id, with cross-source URL
        dedup as a fallback. Returns (event, created).

        If a different source already produced an event with the same
        ``dedup_key`` (a URL-exact match for catalysts), we do NOT insert a
        second row — we record the new source under ``payload.also_seen_in`` and
        return the original. The caller's notification dedup then naturally
        suppresses a second alert.

        Payload refreshes merge-by-default: incoming keys overwrite (including
        explicit ``None`` values — adapters emit their full key set every run,
        so source-owned keys always refresh), while keys absent from the
        incoming payload survive. That is what keeps enrichment / lifecycle /
        user-action keys (``profile``, ``feedback``, ``also_seen_in``,
        ``listing_confirmed``, ``decided_from``/``decided_at``,
        ``financials``, ...) alive across the every-interval source syncs
        without a per-key allowlist. A source that needs a key cleared must
        emit it explicitly (e.g. with a ``None`` value) rather than omit it."""
        existing = await self.get_by_source_event_id(event.source_event_id)
        if existing is not None:
            existing.title = event.title or existing.title
            existing.event_date = event.event_date or existing.event_date
            if event.payload:
                # Merge-by-default (see docstring): a hardcoded allowlist of
                # preserved keys lived here before and needed two bug-driven
                # additions (also_seen_in, then listing_confirmed) while still
                # missing others (decided_from/decided_at, financials) — any
                # key not present in the incoming source payload now survives.
                existing.payload = {**(existing.payload or {}), **event.payload}
            existing.source_url = event.source_url or existing.source_url
            self.session.add(existing)
            await self.session.flush()
            return existing, False

        dupe = await self.get_by_dedup_key(event.dedup_key)
        if dupe is not None:
            # Same story, different source — append provenance, don't re-alert.
            payload = dict(dupe.payload or {})
            seen = list(payload.get("also_seen_in", []))
            seen.append(
                {
                    "source": event.source_name,
                    "url": event.source_url,
                    "source_event_id": event.source_event_id,
                    "via": "url",
                }
            )
            payload["also_seen_in"] = seen
            dupe.payload = payload
            self.session.add(dupe)
            await self.session.flush()
            return dupe, False

        self.session.add(event)
        await self.session.flush()
        return event, True

    async def list_recent(self, limit: int = 50) -> list[Event]:
        result = await self.session.execute(
            select(Event).order_by(Event.created_at.desc()).limit(limit)
        )
        return list(result.scalars().all())

    def _list_events_filter(
        self,
        stmt,
        event_type: str | None,
        relevant: bool,
        period: str,
        date_from: date | None,
        date_to: date | None,
    ):
        if relevant:
            # Only events matched to a tracked company / passing filters.
            # IN-subquery (not JOIN+DISTINCT) so Postgres SELECT DISTINCT
            # does not reject the ORDER BY expression.
            matched_ids = (
                select(EventRelevance.event_id)
                .where(EventRelevance.matched.is_(True))
                .scalar_subquery()
            )
            stmt = stmt.where(Event.id.in_(matched_ids))
        if event_type:
            stmt = stmt.where(Event.event_type == event_type)
        if period == "upcoming":
            stmt = stmt.where(
                Event.event_date.is_not(None), Event.event_date >= func.now()
            )
        elif period == "past":
            stmt = stmt.where(
                Event.event_date.is_not(None), Event.event_date < func.now()
            )
        if date_from is not None:
            stmt = stmt.where(
                Event.event_date.is_not(None),
                Event.event_date >= datetime.combine(date_from, time.min),
            )
        if date_to is not None:
            stmt = stmt.where(
                Event.event_date.is_not(None),
                Event.event_date <= datetime.combine(date_to, time.max),
            )
        return stmt

    async def list_events(
        self,
        event_type: str | None = None,
        limit: int = 100,
        relevant: bool = False,
        period: str = "all",
        date_from: date | None = None,
        date_to: date | None = None,
        offset: int = 0,
    ) -> list[Event]:
        stmt = self._list_events_filter(
            select(Event), event_type, relevant, period, date_from, date_to
        )
        stmt = (
            stmt.order_by(Event.event_date.is_(None), Event.event_date)
            .offset(max(offset, 0))
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def count_events(
        self,
        event_type: str | None = None,
        relevant: bool = False,
        period: str = "all",
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> int:
        """Total rows matching the same filters as `list_events` — drives
        server-side pagination's page count."""
        stmt = self._list_events_filter(
            select(func.count()).select_from(Event),
            event_type,
            relevant,
            period,
            date_from,
            date_to,
        )
        result = await self.session.execute(stmt)
        return int(result.scalar_one())

    async def count_by_type(self, event_type: str) -> int:
        result = await self.session.execute(
            select(func.count()).select_from(Event).where(Event.event_type == event_type)
        )
        return int(result.scalar_one())

    async def count_tracked_upcoming(
        self, event_type: str, pairs: list[tuple[str, str]]
    ) -> int:
        """Upcoming events for `event_type` whose (symbol, exchange) matches
        a tracked company. Returns 0 with no pairs so the dashboard shows
        the truthful zero when nothing is tracked."""
        if not pairs:
            return 0
        result = await self.session.execute(
            select(func.count())
            .select_from(Event)
            .where(
                Event.event_type == event_type,
                Event.event_date.is_not(None),
                Event.event_date >= func.now(),
                tuple_(Event.symbol, Event.exchange).in_(pairs),
            )
        )
        return int(result.scalar_one())

    async def recent_quarterly_actuals(
        self,
        symbol: str,
        exchange: str,
        limit: int = 4,
        exclude_event_id: int | None = None,
    ) -> list[tuple[Any, Any, Any, Any]]:
        """Last `limit` REPORTED earnings (rows where `actual` is non-null)
        for one company, oldest first. Returns
        (fiscal_period_end, estimate, actual, percent) tuples for the
        results-card beat-streak line. Excludes the currently-reporting
        row so the streak shows *prior* quarters.

        The actual-non-null check stays in Python: JSON-null vs missing
        key semantics differ between Postgres JSON/JSONB and SQLite's
        json_extract, so an in-SQL filter can't be expressed uniformly
        without sacrificing portability."""
        stmt = (
            select(Event)
            .where(
                Event.event_type == "earnings",
                Event.symbol == symbol,
                Event.exchange == exchange,
                Event.event_date.is_not(None),
            )
            .order_by(Event.event_date.desc())
            .limit(limit * 4)  # over-fetch — many rows will have null actual
        )
        result = await self.session.execute(stmt)
        rows: list[tuple[Any, Any, Any, Any]] = []
        for e in result.scalars().all():
            if exclude_event_id is not None and e.id == exclude_event_id:
                continue
            p = e.payload or {}
            if p.get("actual") is None:
                continue
            rows.append(
                (p.get("fiscal_period_end"), p.get("estimate"), p.get("actual"), p.get("percent"))
            )
            if len(rows) >= limit:
                break
        rows.reverse()  # oldest first for left-to-right display
        return rows

    async def ipo_exchange_counts(self) -> list[tuple[str | None, str | None, int]]:
        """(source_name, exchange, count) per IPO source+exchange, busiest first.

        Grouped by source too because the same logical market can arrive from
        different feeds (CN from akshare, US from EODHD) — the coverage table
        shows provenance, not just the exchange code.
        """
        result = await self.session.execute(
            select(Event.source_name, Event.exchange, func.count())
            .where(Event.event_type == "ipo")
            .group_by(Event.source_name, Event.exchange)
            .order_by(func.count().desc())
        )
        return [(row[0], row[1], int(row[2])) for row in result.all()]

    async def get(self, event_id: int) -> Event | None:
        return await self.session.get(Event, event_id)

    async def list_by_type_status(
        self, event_type: str, status: str, limit: int = 100, offset: int = 0
    ) -> list[Event]:
        result = await self.session.execute(
            select(Event)
            .where(Event.event_type == event_type, Event.status == status)
            .order_by(Event.created_at.desc())
            .limit(min(limit, 1000))
            .offset(offset)
        )
        return list(result.scalars().all())

    async def count_by_type_status(self, event_type: str, status: str) -> int:
        result = await self.session.execute(
            select(func.count())
            .select_from(Event)
            .where(Event.event_type == event_type, Event.status == status)
        )
        return int(result.scalar_one())

    async def set_feedback(self, event: Event, label: str) -> Event:
        payload = dict(event.payload or {})
        payload["feedback"] = label
        event.payload = payload
        self.session.add(event)
        await self.session.commit()
        await self.session.refresh(event)
        return event

    async def update_status_and_payload(
        self,
        event: Event,
        *,
        status: str,
        payload_merge: dict[str, Any] | None = None,
        payload_unset: list[str] | None = None,
    ) -> Event:
        """Single-shot status + payload update used by the /decide endpoint.
        Keeps the (status, decided_from, decided_at, feedback) write atomic
        so the row can never be 'notified without decided_from'."""
        event.status = status
        payload = dict(event.payload or {})
        if payload_merge:
            payload.update(payload_merge)
        if payload_unset:
            for k in payload_unset:
                payload.pop(k, None)
        event.payload = payload
        self.session.add(event)
        await self.session.commit()
        await self.session.refresh(event)
        return event
