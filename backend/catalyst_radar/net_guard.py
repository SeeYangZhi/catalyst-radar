"""SSRF guard for outbound fetches of untrusted URLs.

Some fetch targets are not ours to choose: URLs the LLM cites from
``web_search``, prospectus/report links scraped from exchange pages, and
earnings-report links. A poisoned page could point any of them at an
internal address (cloud metadata ``169.254.169.254``, ``redis:6379``,
``localhost``). ``guarded_client`` returns an ``httpx.AsyncClient`` whose
request hook resolves the target host and refuses anything that is not a
globally routable address — checked on EVERY hop, so a public URL that
redirects to an internal one is refused too.

Residual risk: resolution happens just before httpx connects, so a
DNS-rebinding host with a ~0 TTL could in theory flip between the check
and the connect. Acceptable for this app's threat model.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Any

import httpx


class BlockedDestinationError(httpx.RequestError):
    """Raised (as an ``httpx.HTTPError``) when a request targets a
    non-public address, so existing ``except httpx.HTTPError`` fail-soft
    paths treat it like any other transport failure."""


def _is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


async def _resolve(host: str) -> list[str]:
    """All addresses ``host`` resolves to. Module-level so tests can stub
    DNS without touching the network."""
    infos = await asyncio.get_running_loop().getaddrinfo(
        host, None, type=socket.SOCK_STREAM
    )
    return [info[4][0] for info in infos]


async def assert_public_host(host: str) -> None:
    """Raise ``ValueError`` unless every address ``host`` maps to is public."""
    if not host:
        raise ValueError("missing host")
    try:
        addrs = [ipaddress.ip_address(host.strip("[]"))]
    except ValueError:
        try:
            addrs = [ipaddress.ip_address(a.split("%", 1)[0]) for a in await _resolve(host)]
        except OSError as exc:
            raise ValueError(f"cannot resolve {host}: {exc}") from exc
    if not addrs:
        raise ValueError(f"{host} resolved to no addresses")
    for ip in addrs:
        if not _is_public(ip):
            raise ValueError(f"{host} resolves to non-public address {ip}")


async def _guard_request(request: httpx.Request) -> None:
    try:
        await assert_public_host(request.url.host)
    except ValueError as exc:
        raise BlockedDestinationError(str(exc), request=request) from exc


def guarded_client(**kwargs: Any) -> httpx.AsyncClient:
    """``httpx.AsyncClient`` that refuses non-public destinations on every
    request and redirect hop. Accepts the usual AsyncClient kwargs."""
    hooks = dict(kwargs.pop("event_hooks", None) or {})
    hooks["request"] = [_guard_request, *hooks.get("request", [])]
    return httpx.AsyncClient(event_hooks=hooks, **kwargs)
