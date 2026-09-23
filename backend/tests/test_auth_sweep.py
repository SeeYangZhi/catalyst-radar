"""Every /api/v1 route must reject unauthenticated requests.

Enumerates the live FastAPI route table so any newly added v1 endpoint is
swept automatically. Additions to PUBLIC_PATHS are a conscious, reviewed
choice — each entry documents why that route may be reached without a
bearer token.
"""

import re

import pytest
from fastapi.routing import APIRoute

from catalyst_radar.main import app

PUBLIC_PATHS: set[tuple[str, str]] = {
    # Login itself — issues the bearer token, so it can't require one.
    ("POST", "/api/v1/auth/token"),
    # Registration is public by design but hard-gated by the
    # `registration_enabled` runtime flag (default False → 403). Allowlisted
    # so the sweep doesn't "pass" for the wrong reason while the flag is off.
    ("POST", "/api/v1/auth/register"),
    # Liveness/readiness probe for nginx, compose healthchecks, uptime
    # monitors — must answer without credentials.
    ("GET", "/api/v1/health"),
    # Telegram update delivery. Telegram cannot send a bearer token; the
    # endpoint is instead guarded by the echoed
    # X-Telegram-Bot-Api-Secret-Token shared secret (constant-time compare).
    ("POST", "/api/v1/telegram/webhook"),
}


def _v1_routes() -> list[tuple[str, str]]:
    found = []
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path.startswith("/api/v1"):
            for method in route.methods - {"HEAD", "OPTIONS"}:
                found.append((method, route.path))
    return sorted(found)


def test_route_inventory_nonempty() -> None:
    """Guard the sweep itself: if route enumeration breaks (mount prefix
    change, router refactor), the parametrized test would silently collect
    zero cases. Also ensure every allowlisted path still exists."""
    routes = set(_v1_routes())
    assert len(routes) > 10
    missing = PUBLIC_PATHS - routes
    assert not missing, f"PUBLIC_PATHS entries no longer exist: {sorted(missing)}"


@pytest.mark.parametrize("method,path", _v1_routes())
async def test_route_requires_auth(client, method: str, path: str) -> None:
    if (method, path) in PUBLIC_PATHS:
        pytest.skip("intentionally public — see PUBLIC_PATHS")
    concrete = re.sub(r"\{[^}]+\}", "1", path)
    resp = await client.request(method, concrete)
    assert resp.status_code in (401, 403), (
        f"{method} {path} returned {resp.status_code} for an unauthenticated "
        "request — every non-public v1 route must reject missing credentials"
    )
