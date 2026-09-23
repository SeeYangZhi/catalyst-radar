from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel

from catalyst_radar.api.deps import CurrentUser, SessionDep
from catalyst_radar.repositories.source_repository import SourceRunRepository

router = APIRouter(prefix="/source-runs", tags=["source-runs"])


class SourceRunOut(BaseModel):
    id: int
    source_name: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    item_count: int
    error_count: int
    last_error: str | None
    # Per-run telemetry counters, e.g. {"url_probes": {...}} .
    summary: dict[str, Any] | None = None

    model_config = {"from_attributes": True}


@router.get("", response_model=list[SourceRunOut])
async def list_source_runs(
    current_user: CurrentUser,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[SourceRunOut]:
    rows = await SourceRunRepository(session).list_recent(limit)
    return [SourceRunOut.model_validate(r) for r in rows]
