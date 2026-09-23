"""URL-liveness probe behavior tests.

Covers the keep/drop policy:
- 2xx / 3xx → keep
- 401 / 403 / 429 → keep (paywall/rate-limit; page still exists)
- 404 / 410 → drop
- 5xx → retry once, drop on second 5xx
- 405 → fall back to GET Range, decide on that
- timeout / connection error → drop
- empty / dupe URLs handled

The probe is patched via httpx.MockTransport — the same pattern
test_structured_outputs.py uses for the OpenAI client.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from catalyst_radar.services import url_liveness


def _patch(monkeypatch, handler) -> None:
    """Replace the httpx.AsyncClient that url_liveness instantiates so
    requests go through our MockTransport rather than the network."""
    real_client_cls = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client_cls(*args, **kwargs)

    monkeypatch.setattr(url_liveness.httpx, "AsyncClient", factory)


@pytest.mark.asyncio
async def test_keep_2xx_drop_404(monkeypatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if "good" in str(req.url):
            return httpx.Response(200)
        return httpx.Response(404)

    _patch(monkeypatch, handler)
    live = await url_liveness.filter_live(
        ["https://x/good", "https://x/bad"]
    )
    assert live == {"https://x/good"}


@pytest.mark.asyncio
async def test_keep_paywall_codes(monkeypatch) -> None:
    """401/403/429 mean the page exists — the user may have credentials
    we don't (Bloomberg/FT/Reuters). Keep the citation."""
    codes = {
        "https://x/a": 401,
        "https://x/b": 403,
        "https://x/c": 429,
    }

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(codes[str(req.url)])

    _patch(monkeypatch, handler)
    live = await url_liveness.filter_live(list(codes))
    assert live == set(codes)


@pytest.mark.asyncio
async def test_405_falls_back_to_get_range(monkeypatch) -> None:
    """A server that rejects HEAD with 405 must be re-probed with GET."""
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.method)
        if req.method == "HEAD":
            return httpx.Response(405)
        # GET should carry the bandwidth-saving Range header.
        assert req.headers.get("range") == "bytes=0-1023"
        return httpx.Response(206)  # Partial Content

    _patch(monkeypatch, handler)
    live = await url_liveness.filter_live(["https://x/picky"])
    assert live == {"https://x/picky"}
    assert seen == ["HEAD", "GET"]


@pytest.mark.asyncio
async def test_5xx_retries_once_then_drops(monkeypatch) -> None:
    calls: dict[str, int] = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503)

    _patch(monkeypatch, handler)
    live = await url_liveness.filter_live(["https://x/flap"])
    assert live == set()
    # First HEAD (5xx) → one retry GET (5xx) → drop.
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_5xx_then_2xx_keeps(monkeypatch) -> None:
    n = {"i": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        n["i"] += 1
        return httpx.Response(503 if n["i"] == 1 else 200)

    _patch(monkeypatch, handler)
    live = await url_liveness.filter_live(["https://x/flap"])
    assert live == {"https://x/flap"}


@pytest.mark.asyncio
async def test_timeout_drops(monkeypatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("boom")

    _patch(monkeypatch, handler)
    live = await url_liveness.filter_live(["https://x/dead"])
    assert live == set()


@pytest.mark.asyncio
async def test_empty_and_dedup(monkeypatch) -> None:
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        return httpx.Response(200)

    _patch(monkeypatch, handler)
    live = await url_liveness.filter_live(
        ["", "https://x/a", "https://x/a", "  ", "https://x/b"]
    )
    assert live == {"https://x/a", "https://x/b"}
    # Probed each unique URL exactly once.
    assert sorted(seen) == ["https://x/a", "https://x/b"]


@pytest.mark.asyncio
async def test_filter_live_sources_preserves_order(monkeypatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200 if "good" in str(req.url) else 404)

    _patch(monkeypatch, handler)
    out = await url_liveness.filter_live_sources(
        [
            {"name": "A", "url": "https://x/good-1"},
            {"name": "B", "url": "https://x/bad"},
            {"name": "C", "url": "https://x/good-2"},
        ]
    )
    assert [s["name"] for s in out] == ["A", "C"]


@pytest.mark.asyncio
async def test_concurrent_probes(monkeypatch) -> None:
    """Probes should run concurrently — total wall time well under the
    sum of per-URL delays. Three 0.4s probes in serial = 1.2s; concurrent
    should land comfortably under 0.9s with margin for scheduler jitter."""

    async def handler(req: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.4)
        return httpx.Response(200)

    _patch(monkeypatch, handler)
    start = asyncio.get_event_loop().time()
    live = await url_liveness.filter_live(
        ["https://x/a", "https://x/b", "https://x/c"]
    )
    elapsed = asyncio.get_event_loop().time() - start
    assert live == {"https://x/a", "https://x/b", "https://x/c"}
    assert elapsed < 0.9, f"probes ran serially? elapsed={elapsed:.2f}s"


@pytest.mark.asyncio
async def test_waf_403_dropped_when_origin_blocks(monkeypatch) -> None:
    """a Cloudflare 403 that STAYS blocked on the browser-UA retry
    is a bot-block we can't confirm → drop (no false-keep)."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(403, headers={"cf-ray": "abc123", "server": "cloudflare"})

    _patch(monkeypatch, handler)
    live = await url_liveness.filter_live(["https://waf.example/dead"])
    assert live == set()


@pytest.mark.asyncio
async def test_waf_403_kept_when_browser_ua_served(monkeypatch) -> None:
    """A WAF 403 to our probe UA, but the origin serves a browser UA → the
    page exists, keep it."""

    def handler(req: httpx.Request) -> httpx.Response:
        if "Chrome" in req.headers.get("user-agent", ""):
            return httpx.Response(206)  # Range GET partial — page exists
        return httpx.Response(403, headers={"cf-ray": "abc123", "server": "cloudflare"})

    _patch(monkeypatch, handler)
    live = await url_liveness.filter_live(["https://waf.example/real"])
    assert live == {"https://waf.example/real"}


@pytest.mark.asyncio
async def test_waf_403_dropped_when_browser_ua_404s(monkeypatch) -> None:
    """A WAF 403 whose origin 404s for a browser UA → the page is gone, drop."""

    def handler(req: httpx.Request) -> httpx.Response:
        if "Chrome" in req.headers.get("user-agent", ""):
            return httpx.Response(404)
        return httpx.Response(403, headers={"x-iinfo": "9-1-2", "server": "Imperva"})

    _patch(monkeypatch, handler)
    live = await url_liveness.filter_live(["https://waf.example/gone"])
    assert live == set()


@pytest.mark.asyncio
async def test_plain_403_kept_after_escalation(monkeypatch) -> None:
    """A plain (non-WAF) 403 that stays 403 even for a browser UA is a
    genuine paywall/forbidden → kept. Preserves Bloomberg/FT/Reuters."""
    seen_uas: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen_uas.append(req.headers.get("user-agent", ""))
        return httpx.Response(403)  # no WAF headers, any UA

    _patch(monkeypatch, handler)
    live = await url_liveness.filter_live(["https://paywall.example/article"])
    assert live == {"https://paywall.example/article"}
    # We DO escalate every 403 now (allowlisting WAFs is too brittle); the
    # plain 403 is kept because the re-probe carries no WAF signature.
    assert any("Chrome" in ua for ua in seen_uas)


@pytest.mark.asyncio
async def test_416_kept_on_unsatisfiable_range(monkeypatch) -> None:
    """A WAF 403 whose origin returns 416 to the Range GET still proves the
    resource exists → keep (don't drop a live page just because Range was
    rejected)."""

    def handler(req: httpx.Request) -> httpx.Response:
        if "Chrome" in req.headers.get("user-agent", ""):
            return httpx.Response(416)  # Range Not Satisfiable, but page exists
        return httpx.Response(403, headers={"cf-ray": "abc123", "server": "cloudflare"})

    _patch(monkeypatch, handler)
    live = await url_liveness.filter_live(["https://waf.example/ranged"])
    assert live == {"https://waf.example/ranged"}


@pytest.mark.asyncio
async def test_5xx_then_plain_403_kept(monkeypatch) -> None:
    """A transient 5xx followed by a plain (non-WAF) 403 on retry routes
    through the WAF-aware resolver and is kept, not dropped."""
    n = {"i": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        n["i"] += 1
        if n["i"] == 1:
            return httpx.Response(503)  # first HEAD
        return httpx.Response(403)  # retry GET + escalation GET: plain 403

    _patch(monkeypatch, handler)
    live = await url_liveness.filter_live(["https://x/flap-paywall"])
    assert live == {"https://x/flap-paywall"}
