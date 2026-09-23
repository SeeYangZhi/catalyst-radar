from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from catalyst_radar import (
    models,  # noqa: F401 - register all tables
    net_guard,
)
from catalyst_radar.api.deps import get_session
from catalyst_radar.main import app
from catalyst_radar.models.user import User
from catalyst_radar.security import hash_password

TEST_ADMIN_EMAIL = "admin@radar.local"
TEST_ADMIN_PASSWORD = "admin123!"


@pytest.fixture(autouse=True)
def _stub_dns(monkeypatch):
    """Tests never hit real DNS: the SSRF guard resolves every hostname to a
    public documentation address. IP-literal URLs are still checked for real,
    and tests of the guard itself override this stub."""

    async def resolve(host: str) -> list[str]:
        return ["93.184.216.34"]

    monkeypatch.setattr(net_guard, "_resolve", resolve)


@pytest_asyncio.fixture
async def session_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        s.add(
            User(
                email=TEST_ADMIN_EMAIL,
                hashed_password=hash_password(TEST_ADMIN_PASSWORD),
                full_name="Catalyst Radar Admin",
                role="admin",
            )
        )
        await s.commit()
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncClient]:
    async def _override() -> AsyncGenerator[AsyncSession]:
        async with session_factory() as s:
            yield s

    app.dependency_overrides[get_session] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def db_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncSession]:
    async with session_factory() as s:
        yield s


@pytest.fixture
def admin_credentials() -> dict[str, str]:
    return {"username": TEST_ADMIN_EMAIL, "password": TEST_ADMIN_PASSWORD}


@pytest.fixture(autouse=True)
def _reset_login_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-test reset of the login limiter so back-to-back test cases that
    authenticate as admin don't trip the 5/min ceiling. Also pins the
    limiter to its in-memory backend (Redis returns None → fall back) so the
    suite is deterministic whether or not a dev has a live Redis on
    localhost:6379. Tests that exercise the Redis path override this."""
    from catalyst_radar.api.v1 import auth as auth_module

    auth_module._login_attempts.clear()

    async def _force_inmemory(keys: list[str]) -> None:
        return None

    monkeypatch.setattr(auth_module, "_redis_within_limit", _force_inmemory)


@pytest.fixture(autouse=True)
def _pin_gap_websearch_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """gap-always-on web_search  defaults ON in prod so Taiwan/CN-pre-IPO
    names get their only news source. Pin it OFF for the suite so tests that
    create gap-exchange companies without injecting a web_search stub stay
    deterministic regardless of whether a local .env configures OpenAI (which
    would make the default adapter live). Tests asserting the gap sweep opt in
    explicitly."""
    from catalyst_radar.config import settings

    monkeypatch.setattr(settings, "catalyst_websearch_gap_always_on", False)
    # EODHD-news skip (TW/CN gap exchanges) defaults ON in prod to save quota;
    # pin it OFF so existing tests that track TW/CN names with an EODHD stub
    # still see the fetch. The skip's own tests set this explicitly.
    monkeypatch.setattr(settings, "catalyst_eodhd_news_skip_exchanges", "")
    # MOPS Taiwan announcements  default ON in prod. Pin OFF so
    # tests that track TW names without injecting a MOPS stub never hit the
    # live endpoint. MOPS tests opt in explicitly.
    monkeypatch.setattr(settings, "catalyst_mops_news_enabled", False)


@pytest.fixture(autouse=True)
def _freeze_catalyst_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the catalyst pipeline's freshness clock. The catalyst news fixtures
    carry fixed mid-2026 dates; the freshness gate drops anything older than
    ``catalyst_news_max_age_days`` (21d) relative to "now", so without this the
    suite rots as the real wall clock advances past those dates. Only
    ``catalyst_sync``'s notion of now is frozen — other modules are untouched."""
    from datetime import UTC, datetime

    from catalyst_radar.services import catalyst_sync

    frozen = datetime(2026, 6, 4, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(catalyst_sync, "utcnow", lambda: frozen)
