"""URL liveness verification for LLM-cited sources.

The OpenAI Responses API's hosted ``web_search`` tool returns URLs the LLM
*cites*. When the search surfaces nothing genuine, the LLM sometimes
extrapolates plausible URLs from a site's template — an IR site's
``FinanceReport/{Year}Q{N}EN.pdf`` pattern is the canonical failure mode.
A catalyst alert that cites a 404 is worse than a catalyst alert with no
citation, so we probe every cited URL once before persisting.

Policy:
- 2xx, 3xx                  → keep (page exists, possibly after redirect)
- 401, 429                  → keep (auth challenge / rate-limit; page exists
                               for anyone with credentials, which the user
                               likely has for Bloomberg/FT/Reuters)
- 403                       → ambiguous , so always re-probe with a
                               browser-like UA (vendor headers are too
                               incomplete to allowlist — Fastly, AWS WAF,
                               DataDome, … aren't covered). Keep if the
                               origin then serves the page (2xx/3xx, or 416
                               for an unsatisfiable Range on a real resource);
                               drop on 404/410; if it stays blocked, keep only
                               a *plain* (non-WAF) 403 — a genuine
                               paywall/forbidden (Bloomberg/FT) — and drop a
                               WAF-edge block as unconfirmable. A cited-but-
                               unreachable URL is worse than no citation.
- 404, 410                  → drop (page does not exist)
- 416                       → keep (resource exists; only the Range is
                               unsatisfiable — origins that ignore Range)
- 5xx                       → retry once (with GET), then drop
- DNS / timeout / conn err  → drop

The probe is HEAD with redirect-follow; on 405/501 (server rejects HEAD)
it falls back to a 1KB Range GET so the bandwidth stays trivial. Probes
run concurrently, bounded by a short per-URL timeout, so the total
enrichment latency budget stays small. Every hop goes through
``net_guard`` so an LLM-cited URL can never reach an internal address
(blocked → treated as a transport failure → drop).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx

from catalyst_radar.logging import get_logger
from catalyst_radar.net_guard import guarded_client

log = get_logger(__name__)


@dataclass(slots=True)
class ProbeOutcome:
    """Per-URL probe verdict. Surfaced to callers so probe telemetry can be
    persisted durably  — the structlog events alone live only in
    container logs, which are wiped on every deploy."""

    url: str
    status: int | None  # final HTTP status observed; None on transport failure
    kept: bool


_USER_AGENT = "Mozilla/5.0 (compatible; CatalystRadarLinkProbe/1.0)"
# A real browser UA used to re-probe a WAF-blocked 403 . Many bot
# managers serve the origin to a browser-shaped request but 403 our probe.
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_KEEP_STATUS = {401, 416, 429}
_DROP_STATUS = {404, 410}
_PROBE_TIMEOUT_SECONDS = 8.0
_CONNECT_TIMEOUT_SECONDS = 4.0

# Response headers that mark a WAF/CDN edge. A 403 carrying any of these is a
# bot-block, not proof the page is missing — so we escalate rather than keep.
_WAF_HEADER_KEYS = (
    "cf-ray",  # Cloudflare
    "cf-mitigated",  # Cloudflare challenge
    "x-akamai-transformed",  # Akamai
    "x-iinfo",  # Imperva / Incapsula
    "x-sucuri-id",  # Sucuri
    "x-amz-cf-id",  # CloudFront
)
_WAF_SERVER_MARKERS = ("cloudflare", "akamai", "sucuri", "incapsula", "imperva")


def _is_waf_response(resp: httpx.Response) -> bool:
    """True if the response looks like it came from a WAF/CDN edge (used to
    decide whether a 403 is a bot-block vs a genuine paywall)."""
    headers = resp.headers  # httpx.Headers lookups are case-insensitive
    if any(key in headers for key in _WAF_HEADER_KEYS):
        return True
    if any(marker in headers.get("server", "").lower() for marker in _WAF_SERVER_MARKERS):
        return True
    return "cloudfront" in headers.get("via", "").lower()


def _verdict_from_status(code: int) -> bool | None:
    """Return True (keep), False (drop), or None (caller should retry).
    5xx is the only retryable case — many 5xx are transient. 403 is handled
    separately in ``_probe_one`` (WAF-aware), so it is NOT a keep here."""
    if code in _DROP_STATUS:
        return False
    if 200 <= code < 400:
        return True
    if code in _KEEP_STATUS:
        return True
    if 500 <= code < 600:
        return None
    return False


async def _resolve_403(client: httpx.AsyncClient, url: str) -> bool:
    """Decide a 403 . Always re-probe with a browser UA — many
    WAFs/origins 403 our probe UA but serve a browser, and the vendor-header
    set is too incomplete to allowlist which 403s to escalate. Keep if the
    origin then serves the page (2xx/3xx, or 416 for an unsatisfiable Range
    on a real resource); drop if it's gone (404/410); if it stays blocked,
    keep only a *plain* non-WAF 403 (genuine paywall/forbidden) and drop a
    WAF-edge block as unconfirmable."""
    try:
        retry = await client.get(
            url,
            follow_redirects=True,
            headers={
                "User-Agent": _BROWSER_UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Range": "bytes=0-1023",
            },
        )
    except (httpx.HTTPError, httpx.InvalidURL):
        return False
    code = retry.status_code
    if (200 <= code < 400) or code == 416:
        return True
    if code in _DROP_STATUS:
        return False
    # Still 401/403/etc.: a genuine paywall (no WAF edge) is kept; a WAF
    # block can't confirm the page exists, so drop.
    return not _is_waf_response(retry)


async def _decide(client: httpx.AsyncClient, url: str, resp: httpx.Response) -> bool | None:
    """Map a probed response to keep/drop, or None to signal 'retry once'.
    403 routes through the WAF-aware resolver; everything else via
    ``_verdict_from_status``."""
    if resp.status_code == 403:
        return await _resolve_403(client, url)
    return _verdict_from_status(resp.status_code)


async def _probe_one(client: httpx.AsyncClient, url: str) -> ProbeOutcome:
    """One URL → ProbeOutcome (keep/drop + final status). Never raises."""
    try:
        resp = await client.head(url, follow_redirects=True)
    except (httpx.HTTPError, httpx.InvalidURL):
        return ProbeOutcome(url=url, status=None, kept=False)

    if resp.status_code in (405, 501):
        try:
            resp = await client.get(
                url,
                follow_redirects=True,
                headers={"Range": "bytes=0-1023"},
            )
        except (httpx.HTTPError, httpx.InvalidURL):
            return ProbeOutcome(url=url, status=None, kept=False)

    verdict = await _decide(client, url, resp)
    if verdict is not None:
        _log_outcome(url, resp.status_code, kept=verdict)
        return ProbeOutcome(url=url, status=resp.status_code, kept=verdict)

    # 5xx → one retry. GET (not HEAD) so a HEAD-hostile origin isn't double-
    # penalised, and re-run the same decision (incl. WAF-aware 403 handling).
    try:
        resp = await client.get(url, follow_redirects=True, headers={"Range": "bytes=0-1023"})
    except (httpx.HTTPError, httpx.InvalidURL):
        return ProbeOutcome(url=url, status=None, kept=False)
    final_kept = (await _decide(client, url, resp)) is True
    _log_outcome(url, resp.status_code, kept=final_kept)
    return ProbeOutcome(url=url, status=resp.status_code, kept=final_kept)


def _log_outcome(url: str, status: int, *, kept: bool) -> None:
    """Telemetry hook for monitoring. We watch ``url_probe_kept``
    with ``status`` in {401, 403} to flag WAF-fronted hosts that pass our
    keep filter despite the page possibly not existing. Drops are logged
    too so the host-distribution of 404s is observable."""
    if kept and status not in (200, 201, 202, 203, 204, 205, 206):
        log.info("url_probe_kept", url=url, status=status)
    elif not kept:
        log.info("url_probe_dropped", url=url, status=status)


async def probe_urls(urls: list[str]) -> list[ProbeOutcome]:
    """Probe each non-empty URL concurrently and return the per-URL outcome
    (final status + keep/drop). Never raises. ``filter_live`` is the
    set-of-live-URLs convenience view over this."""
    seen: list[str] = []
    seen_set: set[str] = set()
    for raw in urls:
        u = (raw or "").strip()
        if not u.startswith(("http://", "https://")) or u in seen_set:
            continue
        seen.append(u)
        seen_set.add(u)
    if not seen:
        return []

    timeout = httpx.Timeout(_PROBE_TIMEOUT_SECONDS, connect=_CONNECT_TIMEOUT_SECONDS)
    limits = httpx.Limits(max_connections=8)
    headers = {"User-Agent": _USER_AGENT}
    async with guarded_client(
        timeout=timeout, limits=limits, headers=headers, http2=False
    ) as client:
        results = await asyncio.gather(
            *(_probe_one(client, u) for u in seen),
            return_exceptions=True,
        )

    outcomes: list[ProbeOutcome] = []
    for url, res in zip(seen, results, strict=False):
        if isinstance(res, BaseException):
            log.warning("url_probe_error", url=url, error=repr(res))
            outcomes.append(ProbeOutcome(url=url, status=None, kept=False))
        else:
            outcomes.append(res)
    return outcomes


async def filter_live(urls: list[str]) -> set[str]:
    """Probe each non-empty URL concurrently. Returns the set of URLs
    that are reachable (or paywalled but real). Never raises."""
    return {o.url for o in await probe_urls(urls) if o.kept}


async def filter_live_sources(
    sources: list[dict[str, str]],
) -> list[dict[str, str]]:
    """{name, url} citation list → same list with dead URLs removed.
    Preserves the original order and skips entries that have no URL."""
    if not sources:
        return []
    urls = [s.get("url", "") for s in sources]
    live = await filter_live(urls)
    return [s for s in sources if s.get("url") in live]
