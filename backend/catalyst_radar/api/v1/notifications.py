from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel

from catalyst_radar.api.deps import CurrentUser, SessionDep
from catalyst_radar.repositories.notification_repository import (
    NotificationRepository,
)

router = APIRouter(prefix="/notifications", tags=["notifications"])


class NotificationOut(BaseModel):
    id: int
    event_id: int | None
    channel: str
    chat_id: str | None
    status: str
    skip_reason: str | None
    reminder_window: str | None
    attempts: int
    error: str | None
    sent_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


@router.get("", response_model=list[NotificationOut])
async def list_notifications(
    current_user: CurrentUser,
    session: SessionDep,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[NotificationOut]:
    rows = await NotificationRepository(session).list_recent(limit=limit, status=status_filter)
    return [NotificationOut.model_validate(r) for r in rows]


@router.post("/{notification_id}/approve", response_model=NotificationOut)
async def approve_notification(
    notification_id: int,
    current_user: CurrentUser,
    session: SessionDep,
) -> NotificationOut:
    """Move a review-queued notification into the send queue. The next
    dispatch run delivers it (idempotent fan-out)."""
    repo = NotificationRepository(session)
    n = await repo.get(notification_id)
    if n is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="notification not found")
    if n.status not in ("review", "failed"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"cannot approve a notification in status '{n.status}'",
        )
    n.status = "pending"
    await repo.save(n)
    return NotificationOut.model_validate(n)
