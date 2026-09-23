import pytest
from httpx import AsyncClient


async def test_health_ok(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # Redis isn't running under pytest — stub the ping so the healthcheck
    # reflects DB health, which is what this test cares about.
    from catalyst_radar.api.v1 import health as health_module

    async def _ok() -> bool:
        return True

    monkeypatch.setattr(health_module, "_ping_redis", _ok)
    resp = await client.get("/api/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["redis"] == "ok"


async def test_health_degraded_without_redis(client: AsyncClient) -> None:
    """No monkeypatch — real ping fails (no redis in CI), so the
    component status surfaces 'degraded' / 'unreachable' as designed."""
    resp = await client.get("/api/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["database"] == "ok"
    assert body["redis"] == "unreachable"
    assert body["status"] == "degraded"
