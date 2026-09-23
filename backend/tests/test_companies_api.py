from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from catalyst_radar.models.company import CompanyReference


async def _auth(client: AsyncClient, creds: dict[str, str]) -> dict[str, str]:
    token = (await client.post("/api/v1/auth/token", data=creds)).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


async def _seed_reference(
    factory: async_sessionmaker[AsyncSession],
) -> int:
    async with factory() as s:
        ref = CompanyReference(
            symbol="0700",
            exchange="HK",
            country="HK",
            company_name="Tencent Holdings",
            source="eodhd",
        )
        s.add(ref)
        await s.commit()
        await s.refresh(ref)
        return ref.id


async def test_search_requires_auth(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/companies/search?q=tencent")).status_code == 401


async def test_search_track_and_untrack(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    admin_credentials: dict[str, str],
) -> None:
    ref_id = await _seed_reference(session_factory)
    headers = await _auth(client, admin_credentials)

    search = await client.get("/api/v1/companies/search", params={"q": "tencent"}, headers=headers)
    assert search.status_code == 200
    body = search.json()
    assert body["source"] == "reference"
    assert body["count"] == 1
    assert body["results"][0]["symbol"] == "0700"

    track = await client.post(
        "/api/v1/companies/tracked",
        headers=headers,
        json={"company_reference_id": ref_id, "themes": ["china-tech"]},
    )
    assert track.status_code == 201
    tracked_id = track.json()["id"]

    dupe = await client.post(
        "/api/v1/companies/tracked",
        headers=headers,
        json={"company_reference_id": ref_id},
    )
    assert dupe.status_code == 409

    listed = await client.get("/api/v1/companies/tracked", headers=headers)
    assert [c["symbol"] for c in listed.json()] == ["0700"]

    deleted = await client.delete(f"/api/v1/companies/tracked/{tracked_id}", headers=headers)
    assert deleted.status_code == 204

    assert (await client.get("/api/v1/companies/tracked", headers=headers)).json() == []


async def test_manual_track_validation(
    client: AsyncClient, admin_credentials: dict[str, str]
) -> None:
    headers = await _auth(client, admin_credentials)
    bad = await client.post("/api/v1/companies/tracked", headers=headers, json={"symbol": "X"})
    assert bad.status_code == 422


async def test_manual_track_accepts_pre_ipo_reservation_code(
    client: AsyncClient, admin_credentials: dict[str, str]
) -> None:
    """Pre-IPO Chinese companies live under CSRC reservation codes
    (A-prefix) before their 6-digit listing ticker is assigned. The
    manual-add endpoint must accept these so users can track CXMT-like
    names from the moment they appear on the review-committee calendar."""
    headers = await _auth(client, admin_credentials)
    body = {
        "symbol": "A25310",
        "exchange": "SSE",
        "country": "CN",
        "company_name": "长鑫科技",
    }
    created = await client.post(
        "/api/v1/companies/tracked", headers=headers, json=body
    )
    assert created.status_code == 201
    row = created.json()
    assert row["symbol"] == "A25310"
    assert row["exchange"] == "SSE"
    assert row["company_name"] == "长鑫科技"

    # Idempotent on (exchange, symbol).
    dupe = await client.post(
        "/api/v1/companies/tracked", headers=headers, json=body
    )
    assert dupe.status_code == 409
