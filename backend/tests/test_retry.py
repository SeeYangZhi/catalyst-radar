"""Unit tests for the shared transient-retry helper.

Covers both the sync and async variants plus the ``is_transient`` classifier.
Sleeps are monkeypatched out so the suite never pays a real backoff delay.
"""

from __future__ import annotations

import httpx
import pytest

from catalyst_radar.adapters import _retry


class ChunkedEncodingError(Exception):
    """Stand-in for ``requests.exceptions.ChunkedEncodingError`` — the helper
    classifies it by class *name* so we don't need a hard ``requests`` import.
    Named to match the real class exactly, since the match is by name."""


class _Counter:
    """Callable that fails ``fail_times`` then returns ``value``."""

    def __init__(self, exc: Exception, fail_times: int, value: object = "ok") -> None:
        self.exc = exc
        self.fail_times = fail_times
        self.value = value
        self.calls = 0

    def __call__(self) -> object:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.exc
        return self.value


# ── is_transient classifier ────────────────────────────────────────────────


def test_is_transient_classifies_chunked_encoding_by_name() -> None:
    assert _retry.is_transient(ChunkedEncodingError("Response ended prematurely"))


def test_is_transient_classifies_httpx_timeout() -> None:
    assert _retry.is_transient(httpx.ReadTimeout("slow"))


def test_is_transient_classifies_httpx_transport_error() -> None:
    assert _retry.is_transient(httpx.ConnectError("refused"))


def test_is_transient_rejects_value_error() -> None:
    assert not _retry.is_transient(ValueError("bad input"))


# ── sync retry ─────────────────────────────────────────────────────────────


def test_retry_sync_succeeds_on_second_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_retry.time, "sleep", lambda _s: None)
    fn = _Counter(httpx.ReadTimeout("blip"), fail_times=1, value=42)
    result = _retry.retry_sync(fn, attempts=3, base_delay=0.0)
    assert result == 42
    assert fn.calls == 2


def test_retry_sync_reraises_after_exhausting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_retry.time, "sleep", lambda _s: None)
    boom = ChunkedEncodingError("Response ended prematurely")
    fn = _Counter(boom, fail_times=99)
    with pytest.raises(ChunkedEncodingError):
        _retry.retry_sync(fn, attempts=3, base_delay=0.0)
    assert fn.calls == 3


def test_retry_sync_does_not_retry_permanent(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr(_retry.time, "sleep", lambda s: slept.append(s))
    fn = _Counter(ValueError("permanent"), fail_times=99)
    with pytest.raises(ValueError):
        _retry.retry_sync(fn, attempts=3, base_delay=0.0)
    assert fn.calls == 1  # bubbled immediately, no retry
    assert slept == []


def test_retry_sync_extra_retry_on_type(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_retry.time, "sleep", lambda _s: None)

    class _Custom(Exception):
        pass

    fn = _Counter(_Custom("x"), fail_times=1, value="recovered")
    result = _retry.retry_sync(fn, attempts=3, base_delay=0.0, retry_on=(_Custom,))
    assert result == "recovered"
    assert fn.calls == 2


# ── async retry ────────────────────────────────────────────────────────────


async def test_retry_async_succeeds_on_second_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _no_sleep(_s: float) -> None:
        return None

    monkeypatch.setattr(_retry.asyncio, "sleep", _no_sleep)
    calls = 0

    async def fn() -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("refused")
        return 7

    result = await _retry.retry_async(fn, attempts=3, base_delay=0.0)
    assert result == 7
    assert calls == 2


async def test_retry_async_reraises_after_exhausting(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _no_sleep(_s: float) -> None:
        return None

    monkeypatch.setattr(_retry.asyncio, "sleep", _no_sleep)
    calls = 0

    async def fn() -> int:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("blip")

    with pytest.raises(httpx.ReadTimeout):
        await _retry.retry_async(fn, attempts=2, base_delay=0.0)
    assert calls == 2


async def test_retry_async_does_not_retry_permanent(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []

    async def _track(s: float) -> None:
        slept.append(s)

    monkeypatch.setattr(_retry.asyncio, "sleep", _track)
    calls = 0

    async def fn() -> int:
        nonlocal calls
        calls += 1
        raise ValueError("permanent")

    with pytest.raises(ValueError):
        await _retry.retry_async(fn, attempts=3, base_delay=0.0)
    assert calls == 1
    assert slept == []
