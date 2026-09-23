from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.dedup import dedup_key, source_event_id, stable_hash
from catalyst_radar.models.event import Event
from catalyst_radar.repositories.company_repository import (
    CompanyReferenceRepository,
)
from catalyst_radar.repositories.event_repository import EventRepository


def test_dedup_helpers_are_deterministic() -> None:
    assert stable_hash("a", "b") == stable_hash("a", "b")
    assert stable_hash("a", "b") != stable_hash("a", "c")
    assert source_event_id("eodhd", "earnings", "AAPL.US", "2026-05-07") == (
        "eodhd:earnings:AAPL.US:2026-05-07"
    )
    assert dedup_key("x", None, "y") == dedup_key("x", None, "y")


async def test_event_upsert_is_idempotent(db_session: AsyncSession) -> None:
    repo = EventRepository(db_session)
    sid = source_event_id("eodhd", "earnings", "AAPL.US", "2026-05-07")
    base = dict(
        event_type="earnings",
        source_name="eodhd",
        source_event_id=sid,
        dedup_key=dedup_key(sid),
        symbol="AAPL.US",
    )

    event1, created1 = await repo.upsert(Event(**base, title="Q2"))
    await db_session.commit()
    event2, created2 = await repo.upsert(Event(**base, title="Q2 revised"))
    await db_session.commit()

    assert created1 is True
    assert created2 is False
    assert event1.id == event2.id
    assert event2.title == "Q2 revised"


async def test_company_reference_upsert_and_search(
    db_session: AsyncSession,
) -> None:
    repo = CompanyReferenceRepository(db_session)
    await repo.upsert(
        symbol="0700",
        exchange="HK",
        company_name="Tencent Holdings",
        country="HK",
        source_payload={"Code": "0700"},
    )
    await repo.upsert(symbol="0700", exchange="HK", company_name="Tencent Holdings Ltd")
    await db_session.commit()

    by_key = await repo.get_by_exchange_symbol("HK", "0700")
    assert by_key is not None
    assert by_key.company_name == "Tencent Holdings Ltd"

    results = await repo.search(q="tencent", country="HK")
    assert len(results) == 1
    assert results[0].symbol == "0700"


async def test_settings_put_then_get(
    client: AsyncClient, admin_credentials: dict[str, str]
) -> None:
    token = (await client.post("/api/v1/auth/token", data=admin_credentials)).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    put = await client.put(
        "/api/v1/settings",
        headers=headers,
        json={"telegram_alerts_enabled": False},
    )
    assert put.status_code == 200
    assert put.json()["telegram_alerts_enabled"] is False

    get = await client.get("/api/v1/settings", headers=headers)
    assert get.json()["telegram_alerts_enabled"] is False
