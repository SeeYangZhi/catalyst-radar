import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from catalyst_radar.models.company import TrackedCompany
from catalyst_radar.repositories.company_source_repository import (
    MAX_CONSECUTIVE_FAILURES,
    CompanySourceRepository,
)
from catalyst_radar.services import source_discovery
from catalyst_radar.services.source_discovery import (
    DiscoveredSource,
    DiscoveryResult,
    SourceDiscoveryProvider,
    discover_sources_for_company,
)


async def _auth(client: AsyncClient, creds: dict[str, str]) -> dict[str, str]:
    token = (await client.post("/api/v1/auth/token", data=creds)).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


async def _seed_company(factory: async_sessionmaker[AsyncSession]) -> int:
    async with factory() as s:
        company = TrackedCompany(
            symbol="LITE",
            exchange="US",
            country="US",
            company_name="Lumentum Holdings",
            source="manual",
        )
        s.add(company)
        await s.commit()
        await s.refresh(company)
        return company.id


class _StubProvider(SourceDiscoveryProvider):
    name = "stub"

    def __init__(
        self,
        sources: list[DiscoveredSource],
        *,
        configured: bool = True,
        status: str = "completed",
    ) -> None:
        self._sources = sources
        self._configured = configured
        self._status = status

    @property
    def configured(self) -> bool:
        return self._configured

    async def discover(
        self, *, company_name: str, symbol: str, exchange: str
    ) -> DiscoveryResult:
        return DiscoveryResult(self._status, self._sources, self.name)


# ── Repository ────────────────────────────────────────────────────────


async def test_repo_create_list_and_failure_breaks_source(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    company_id = await _seed_company(session_factory)
    repo = CompanySourceRepository(db_session)

    src = await repo.create(
        tracked_company_id=company_id, kind="rss", url="https://lite.com/ir/rss"
    )
    assert src.status == "active"
    assert src.source == "manual"

    listed = await repo.list_for_company(company_id)
    assert [s.url for s in listed] == ["https://lite.com/ir/rss"]

    for _ in range(MAX_CONSECUTIVE_FAILURES):
        await repo.record_fetch_failure(src.id, "403 forbidden")
    refreshed = await repo.get(src.id)
    assert refreshed.consecutive_failures == MAX_CONSECUTIVE_FAILURES
    assert refreshed.status == "broken"

    # A later success clears the failure count and revives the source.
    await repo.record_fetch_success(src.id, etag="abc", had_new_item=True)
    revived = await repo.get(src.id)
    assert revived.status == "active"
    assert revived.consecutive_failures == 0
    assert revived.etag == "abc"
    assert revived.last_item_at is not None


# ── Discovery service ───────────────────────────────────────────────────


async def test_discovery_persists_and_is_idempotent(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    company_id = await _seed_company(session_factory)
    company = await db_session.get(TrackedCompany, company_id)
    provider = _StubProvider(
        [
            DiscoveredSource(kind="rss", url="https://lite.com/ir/rss", label="IR", confidence=0.9),
            DiscoveredSource(kind="blog", url="https://lite.com/blog"),
        ]
    )

    first = await discover_sources_for_company(db_session, company, provider=provider)
    assert first.status == "completed"
    assert first.created == 2

    sources = await CompanySourceRepository(db_session).list_for_company(company_id)
    assert all(s.needs_review for s in sources)
    assert all(s.source == "discovery_stub" for s in sources)

    # Re-running discovery must not create duplicates.
    second = await discover_sources_for_company(db_session, company, provider=provider)
    assert second.created == 0


async def test_discovery_verified_source_skips_review(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    company_id = await _seed_company(session_factory)
    company = await db_session.get(TrackedCompany, company_id)
    provider = _StubProvider(
        [
            DiscoveredSource(kind="ir_press", url="https://x.com/news", verified=True),
            DiscoveredSource(kind="blog", url="https://x.com/blog", verified=False),
        ]
    )
    await discover_sources_for_company(db_session, company, provider=provider)

    by_url = {
        s.url: s for s in await CompanySourceRepository(db_session).list_for_company(company_id)
    }
    assert by_url["https://x.com/news"].needs_review is False
    assert by_url["https://x.com/blog"].needs_review is True


async def test_discovery_fails_cleanly_when_unconfigured(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    company_id = await _seed_company(session_factory)
    company = await db_session.get(TrackedCompany, company_id)
    summary = await discover_sources_for_company(
        db_session, company, provider=_StubProvider([], configured=False)
    )
    assert summary.status == "failed"
    assert summary.created == 0


# ── API endpoints ─────────────────────────────────────────────────────


async def test_source_crud_endpoints(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    admin_credentials: dict[str, str],
) -> None:
    company_id = await _seed_company(session_factory)
    headers = await _auth(client, admin_credentials)
    base = f"/api/v1/companies/tracked/{company_id}/sources"

    created = await client.post(
        base, headers=headers, json={"kind": "rss", "url": "https://lite.com/ir/rss"}
    )
    assert created.status_code == 201
    source_id = created.json()["id"]

    dupe = await client.post(
        base, headers=headers, json={"kind": "rss", "url": "https://lite.com/ir/rss"}
    )
    assert dupe.status_code == 409

    listed = await client.get(base, headers=headers)
    assert [s["url"] for s in listed.json()] == ["https://lite.com/ir/rss"]

    patched = await client.patch(
        f"/api/v1/companies/sources/{source_id}",
        headers=headers,
        json={"is_active": False, "status": "disabled"},
    )
    assert patched.status_code == 200
    assert patched.json()["is_active"] is False

    deleted = await client.delete(f"/api/v1/companies/sources/{source_id}", headers=headers)
    assert deleted.status_code == 204


async def test_add_source_normalizes_url_for_dedup(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    admin_credentials: dict[str, str],
) -> None:
    company_id = await _seed_company(session_factory)
    headers = await _auth(client, admin_credentials)
    base = f"/api/v1/companies/tracked/{company_id}/sources"

    first = await client.post(base, headers=headers, json={"kind": "rss", "url": "https://x.com/ir/"})
    assert first.status_code == 201
    # Same URL without the trailing slash normalizes to the same value → 409.
    dupe = await client.post(base, headers=headers, json={"kind": "rss", "url": "https://x.com/ir"})
    assert dupe.status_code == 409


async def test_patch_source_url_collision_returns_409_not_500(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    admin_credentials: dict[str, str],
) -> None:
    company_id = await _seed_company(session_factory)
    headers = await _auth(client, admin_credentials)
    base = f"/api/v1/companies/tracked/{company_id}/sources"

    await client.post(base, headers=headers, json={"kind": "rss", "url": "https://x.com/a"})
    b = await client.post(base, headers=headers, json={"kind": "blog", "url": "https://x.com/b"})
    b_id = b.json()["id"]

    # PATCH B's url onto A's url must be a clean 409, not an IntegrityError 500.
    clash = await client.patch(
        f"/api/v1/companies/sources/{b_id}", headers=headers, json={"url": "https://x.com/a"}
    )
    assert clash.status_code == 409


async def test_sources_for_unknown_company_404(
    client: AsyncClient, admin_credentials: dict[str, str]
) -> None:
    headers = await _auth(client, admin_credentials)
    resp = await client.get("/api/v1/companies/tracked/9999/sources", headers=headers)
    assert resp.status_code == 404


async def test_discover_endpoint_uses_provider(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    admin_credentials: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    company_id = await _seed_company(session_factory)
    headers = await _auth(client, admin_credentials)

    stub = _StubProvider([DiscoveredSource(kind="rss", url="https://lite.com/ir/rss")])
    monkeypatch.setattr(source_discovery, "get_discovery_provider", lambda name=None: stub)

    resp = await client.post(
        f"/api/v1/companies/tracked/{company_id}/discover-sources", headers=headers
    )
    assert resp.status_code == 200
    assert resp.json()["created"] == 1


async def test_discover_endpoint_502_when_provider_unconfigured(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    admin_credentials: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    company_id = await _seed_company(session_factory)
    headers = await _auth(client, admin_credentials)

    stub = _StubProvider([], configured=False)
    monkeypatch.setattr(source_discovery, "get_discovery_provider", lambda name=None: stub)

    resp = await client.post(
        f"/api/v1/companies/tracked/{company_id}/discover-sources", headers=headers
    )
    assert resp.status_code == 502
