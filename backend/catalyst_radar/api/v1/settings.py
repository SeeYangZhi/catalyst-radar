from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from catalyst_radar.api.deps import CurrentUser, SessionDep
from catalyst_radar.config import settings as app_settings
from catalyst_radar.logging import get_logger
from catalyst_radar.repositories.config_repository import ConfigRepository
from catalyst_radar.runtime_config import (
    EDITABLE,
    RESTART_REQUIRED,
    coerce_editable,
    effective,
)

router = APIRouter()

log = get_logger(__name__)


class SettingsUpdate(BaseModel):
    # Free-form: only keys present in EDITABLE are accepted.
    model_config = {"extra": "allow"}


class SettingsImport(BaseModel):
    """Backup document produced by GET /settings/export."""

    schema_version: int
    overrides: dict[str, Any]


def _coerce_or_422(items: dict[str, Any]) -> dict[str, Any]:
    """Strictly coerce every value to its EDITABLE type. Any non-coercible
    value fails the whole batch with a 422 naming the offending key(s) —
    nothing may be written."""
    coerced: dict[str, Any] = {}
    errors: list[str] = []
    for key, value in items.items():
        try:
            coerced[key] = coerce_editable(key, value)
        except ValueError as exc:
            errors.append(str(exc))
    if errors:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="; ".join(errors),
        )
    return coerced


async def _payload(session: SessionDep) -> dict[str, Any]:
    eff = await effective(session)
    # Flat shape: editable keys at top level (back-compatible) plus meta.
    return {
        "app_name": app_settings.app_name,
        "environment": app_settings.environment,
        "restart_required_keys": sorted(RESTART_REQUIRED),
        **dict(eff.values),
    }


@router.get("/settings")
async def get_settings(current_user: CurrentUser, session: SessionDep) -> dict[str, Any]:
    return await _payload(session)


@router.put("/settings")
async def update_settings(
    payload: dict[str, Any],
    current_user: CurrentUser,
    session: SessionDep,
) -> dict[str, Any]:
    unknown = [k for k in payload if k not in EDITABLE]
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"unknown settings: {unknown}",
        )
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="no settings provided",
        )
    await ConfigRepository(session).set_many(_coerce_or_422(payload))
    return await _payload(session)


@router.get("/settings/export")
async def export_settings(
    current_user: CurrentUser, session: SessionDep
) -> dict[str, Any]:
    """Versioned backup of runtime overrides, for restore after a DB wipe.

    Only user-editable keys are included — internal `_`-prefixed app_config
    rows (sweep cursors, etc.) stay out of the document.

    Exports are always importable: every stored value is run through the same
    strict coercion the import applies, so legacy values are normalized (e.g.
    a stored "7" for an int key exports as 7) and non-coercible stored values
    are omitted from the document (a warning is logged naming the key; the
    subsequent import simply leaves that key at its default).
    """
    rows = await ConfigRepository(session).all()
    overrides: dict[str, Any] = {}
    for key, value in rows.items():
        if key not in EDITABLE or key.startswith("_"):
            continue
        try:
            overrides[key] = coerce_editable(key, value)
        except ValueError as exc:
            log.warning("settings_export_skipped_key", key=key, error=repr(exc))
    return {"schema_version": 1, "overrides": overrides}


@router.post("/settings/import")
async def import_settings(
    payload: SettingsImport,
    current_user: CurrentUser,
    session: SessionDep,
) -> dict[str, Any]:
    """Restore an exported overrides document. All-or-nothing: every key is
    validated against EDITABLE and every value strictly coerced to the key's
    type before anything is written; the write itself is a single commit."""
    if payload.schema_version != 1:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"unsupported schema_version: {payload.schema_version}",
        )
    bad = sorted(
        k for k in payload.overrides if k not in EDITABLE or k.startswith("_")
    )
    if bad:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"not importable: {bad}",
        )
    coerced = _coerce_or_422(payload.overrides)
    await ConfigRepository(session).set_many(coerced)
    return {"applied": len(coerced)}
