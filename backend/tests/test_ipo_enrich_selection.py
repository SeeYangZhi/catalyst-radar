import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.models.event import Event
from catalyst_radar.models.notification import Notification
from catalyst_radar.services.ipo_sync import (
    _needs_profile,
    _needs_upgrade,
    select_enrich_candidates,
)


def _ipo(profile: dict | None) -> Event:
    return Event(
        event_type="ipo",
        source_name="eodhd.ipos",
        source_event_id=f"sid-{id(profile)}",
        dedup_key=f"dk-{id(profile)}",
        symbol="X",
        payload={"profile": profile} if profile is not None else {},
    )


def test_needs_profile_true_when_no_description() -> None:
    assert _needs_profile(_ipo({"checked": True})) is True
    assert _needs_profile(_ipo({"description": "x"})) is False


def test_needs_upgrade_only_for_unattempted_websearch_provisional() -> None:
    assert _needs_upgrade(_ipo({"description": "x", "description_source": "websearch"})) is True
    attempted = {
        "description": "x",
        "description_source": "websearch",
        "prospectus_attempted": True,
    }
    assert _needs_upgrade(_ipo(attempted)) is False
    assert _needs_upgrade(_ipo({"description": "x", "description_source": "prospectus"})) is False
    assert _needs_upgrade(_ipo({"checked": True})) is False


async def _persist_ipo(session: AsyncSession, sid: str, profile: dict, *, pending: bool) -> Event:
    e = Event(
        event_type="ipo", source_name="eodhd.ipos", source_event_id=sid,
        dedup_key=f"dk-{sid}", symbol=sid, payload={"profile": profile},
    )
    session.add(e)
    await session.flush()
    if pending:
        session.add(
            Notification(event_id=e.id, channel="telegram", dedup_key=f"n-{sid}",
                         status="pending", payload={"text": "x"})
        )
    await session.commit()
    return e


@pytest.mark.asyncio
async def test_imminent_no_description_ranks_above_upgrade_and_backlog(
    db_session: AsyncSession,
) -> None:
    imm_none = await _persist_ipo(db_session, "imm_none", {"checked": True}, pending=True)
    imm_upg = await _persist_ipo(
        db_session, "imm_upg", {"description": "w", "description_source": "websearch"}, pending=True
    )
    backlog = await _persist_ipo(db_session, "backlog", {"checked": True}, pending=False)

    rows = [backlog, imm_upg, imm_none]  # deliberately worst-first
    picked = await select_enrich_candidates(db_session, rows, budget=2)
    ids = [e.id for e in picked]

    assert ids[0] == imm_none.id
    assert imm_upg.id in ids
    assert ids.index(imm_none.id) < ids.index(imm_upg.id)
