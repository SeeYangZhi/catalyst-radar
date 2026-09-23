"""Shared transient-error retry for the source adapters.

Adapters scrape flaky third-party providers (Eastmoney/akshare, EODHD,
CNINFO, AAStocks via Playwright). Transient blips — a connection reset, a
read timeout, a response truncated mid-stream
(``requests.exceptions.ChunkedEncodingError: Response ended prematurely``),
or a Chromium launch hiccup — used to waste a whole sync tick because the
adapters are fail-soft: one exception and they return an empty result.

This module centralizes "retry a small number of times with generous
backoff, but only for transient errors" so the hand-rolled loops scattered
across the akshare adapters collapse to one tested implementation. Permanent
errors (``ValueError``, auth/config errors) bubble immediately — retrying
them only burns the provider's goodwill.

Two entry points, same policy:
  - ``retry_sync``  — for the akshare ``_fetch_blocking`` call sites that run
    under ``asyncio.to_thread``; sleeps with ``time.sleep``.
  - ``retry_async`` — for the ``httpx``/Playwright async call sites; sleeps
    with ``asyncio.sleep`` so the event loop keeps turning.

Backoff is exponential from ``base_delay``, capped at ``max_delay``. Callers
pick conservative numbers (few attempts, generous delays) — Eastmoney
rate-limits aggressive callers, so hammering makes a blip worse.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

import httpx

from catalyst_radar.logging import get_logger

log = get_logger(__name__)

# Exception *types* always treated as transient. ``httpx.TransportError`` is
# the base for ConnectError / ReadError / WriteError / ConnectTimeout / etc.,
# and ``httpx.TimeoutException`` covers the read/connect/pool timeouts.
_TRANSIENT_TYPES: tuple[type[BaseException], ...] = (
    httpx.TimeoutException,
    httpx.TransportError,
)

# Matched by class *name* so we don't take a hard dependency on ``requests``
# (it's only a transitive dep via akshare). ``ChunkedEncodingError`` is what
# akshare surfaces when Eastmoney truncates a paginated response mid-stream.
_TRANSIENT_NAMES: frozenset[str] = frozenset(
    {
        "ChunkedEncodingError",
        "ConnectionResetError",
        "RemoteDisconnected",
        "ProtocolError",
        "IncompleteRead",
    }
)


def is_transient(exc: BaseException) -> bool:
    """Whether ``exc`` looks like a recoverable transport blip (worth a
    retry) rather than a permanent error (bad input, auth, config)."""
    if isinstance(exc, _TRANSIENT_TYPES):
        return True
    if isinstance(exc, ConnectionResetError | ConnectionError):
        return True
    # Match by class name across the inheritance chain so subclasses and the
    # requests/urllib3 exceptions we can't import directly are still caught.
    for klass in type(exc).__mro__:
        if klass.__name__ in _TRANSIENT_NAMES:
            return True
    return False


def _should_retry(
    exc: BaseException, retry_on: tuple[type[BaseException], ...]
) -> bool:
    return is_transient(exc) or (bool(retry_on) and isinstance(exc, retry_on))


def _backoff(attempt: int, base_delay: float, max_delay: float) -> float:
    """Exponential delay for the *just-failed* attempt (0-indexed)."""
    return min(base_delay * (4**attempt), max_delay)


def retry_sync[T](
    fn: Callable[[], T],
    *,
    attempts: int = 3,
    base_delay: float = 5.0,
    max_delay: float = 20.0,
    retry_on: tuple[type[BaseException], ...] = (),
    label: str = "fetch",
) -> T:
    """Call ``fn`` up to ``attempts`` times, retrying only transient errors
    (plus any extra types in ``retry_on``) with exponential backoff. The last
    exception is re-raised once attempts are exhausted; permanent errors
    bubble on the first occurrence.

    Default policy (3 attempts, 5s/20s backoff) mirrors the akshare adapters'
    hand-rolled loops — conservative on purpose, Eastmoney rate-limits."""
    last_exc: BaseException | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - classified below; permanent re-raised
            if not _should_retry(exc, retry_on):
                raise
            last_exc = exc
            if attempt < attempts - 1:
                delay = _backoff(attempt, base_delay, max_delay)
                log.warning(
                    "retry_sync_transient",
                    label=label,
                    attempt=attempt + 1,
                    attempts=attempts,
                    delay=delay,
                    error=repr(exc),
                )
                time.sleep(delay)
    assert last_exc is not None  # loop ran ≥1× and never returned
    raise last_exc


async def retry_async[T](
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 10.0,
    retry_on: tuple[type[BaseException], ...] = (),
    label: str = "fetch",
) -> T:
    """Async twin of ``retry_sync`` — awaits ``fn()`` and backs off with
    ``asyncio.sleep`` so the event loop keeps turning between attempts."""
    last_exc: BaseException | None = None
    for attempt in range(attempts):
        try:
            return await fn()
        except Exception as exc:  # noqa: BLE001 - classified below; permanent re-raised
            if not _should_retry(exc, retry_on):
                raise
            last_exc = exc
            if attempt < attempts - 1:
                delay = _backoff(attempt, base_delay, max_delay)
                log.warning(
                    "retry_async_transient",
                    label=label,
                    attempt=attempt + 1,
                    attempts=attempts,
                    delay=delay,
                    error=repr(exc),
                )
                await asyncio.sleep(delay)
    assert last_exc is not None
    raise last_exc
