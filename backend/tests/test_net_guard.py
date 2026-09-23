"""SSRF guard: untrusted-URL fetches must never reach internal addresses."""

from __future__ import annotations

import httpx
import pytest

from catalyst_radar import net_guard
from catalyst_radar.services import url_liveness


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "10.0.0.5",
        "192.168.1.1",
        "169.254.169.254",
        "::1",
        "::ffff:127.0.0.1",
        "0.0.0.0",
    ],
)
async def test_ip_literals_that_are_not_public_are_refused(host: str) -> None:
    with pytest.raises(ValueError):
        await net_guard.assert_public_host(host)


async def test_public_ip_literal_is_allowed() -> None:
    await net_guard.assert_public_host("93.184.216.34")


async def test_hostname_resolving_to_private_address_is_refused(monkeypatch) -> None:
    async def resolve(host: str) -> list[str]:
        return ["93.184.216.34", "10.1.2.3"]  # any private answer poisons it

    monkeypatch.setattr(net_guard, "_resolve", resolve)
    with pytest.raises(ValueError):
        await net_guard.assert_public_host("rebind.example")


async def test_redirect_to_internal_address_is_blocked() -> None:
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        if req.url.host == "public.example":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/meta"})
        return httpx.Response(200)

    async with net_guard.guarded_client(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        with pytest.raises(net_guard.BlockedDestinationError):
            await client.get("https://public.example/page")
    assert seen == ["https://public.example/page"]  # internal hop never sent


async def test_liveness_probe_drops_internal_url_without_requesting_it(monkeypatch) -> None:
    requested: list[str] = []
    real_client_cls = httpx.AsyncClient

    def factory(*args, **kwargs):
        def handler(req: httpx.Request) -> httpx.Response:
            requested.append(str(req.url))
            return httpx.Response(200)

        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client_cls(*args, **kwargs)

    monkeypatch.setattr(url_liveness.httpx, "AsyncClient", factory)
    outcomes = await url_liveness.probe_urls(["http://127.0.0.1:6379/", "https://news.example/a"])
    by_url = {o.url: o.kept for o in outcomes}
    assert by_url == {"http://127.0.0.1:6379/": False, "https://news.example/a": True}
    assert requested == ["https://news.example/a"]
