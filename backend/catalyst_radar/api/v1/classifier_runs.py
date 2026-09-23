from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel

from catalyst_radar.api.deps import CurrentUser, SessionDep
from catalyst_radar.repositories.classifier_repository import (
    ClassifierRunRepository,
)

router = APIRouter(prefix="/classifier-runs", tags=["classifier-runs"])


class ClassifierRunOut(BaseModel):
    id: int
    model: str
    prompt_version: str
    status: str
    response_id: str | None
    input_tokens: int | None
    output_tokens: int | None
    is_company_critical: bool | None
    output: dict[str, Any] | None
    error: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


@router.get("", response_model=list[ClassifierRunOut])
async def list_classifier_runs(
    current_user: CurrentUser,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[ClassifierRunOut]:
    rows = await ClassifierRunRepository(session).list_recent(limit)
    return [ClassifierRunOut.model_validate(r) for r in rows]
