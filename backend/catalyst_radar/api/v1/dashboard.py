from datetime import datetime
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from catalyst_radar.api.deps import CurrentUser, SessionDep
from catalyst_radar.repositories.company_repository import (
    TrackedCompanyRepository,
)
from catalyst_radar.repositories.event_repository import EventRepository
from catalyst_radar.repositories.notification_repository import (
    TelegramChatRepository,
)
from catalyst_radar.repositories.source_repository import SourceRunRepository

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


class LastRun(BaseModel):
    source_name: str
    status: str
    started_at: datetime
    item_count: int


class DashboardSummary(BaseModel):
    tracked_companies: int
    earnings_events: int
    ipo_events: int
    catalyst_events: int
    catalysts_in_review: int
    telegram_chats: int
    last_source_run: LastRun | None


@router.get("/summary", response_model=DashboardSummary)
async def dashboard_summary(current_user: CurrentUser, session: SessionDep) -> Any:
    tracked = await TrackedCompanyRepository(session).list_active()
    events = EventRepository(session)
    review = await events.list_by_type_status("catalyst", "review", 200)
    chats = await TelegramChatRepository(session).recipients()
    runs = await SourceRunRepository(session).list_recent(1)

    # Dashboard cards are personal: only count upcoming events for tickers
    # the user actually tracks (matched on symbol AND exchange — a ticker
    # can list on multiple exchanges with separate earnings/IPOs).
    tracked_pairs = [(tc.symbol, tc.exchange) for tc in tracked]

    last = None
    if runs:
        r = runs[0]
        last = LastRun(
            source_name=r.source_name,
            status=r.status,
            started_at=r.started_at,
            item_count=r.item_count,
        )

    return DashboardSummary(
        tracked_companies=len(tracked),
        earnings_events=await events.count_tracked_upcoming("earnings", tracked_pairs),
        ipo_events=await events.count_tracked_upcoming("ipo", tracked_pairs),
        catalyst_events=await events.count_by_type("catalyst"),
        catalysts_in_review=len(review),
        telegram_chats=len(chats),
        last_source_run=last,
    )
