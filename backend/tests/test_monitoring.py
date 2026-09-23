"""Self-monitoring watchdog tests (deep-analysis task 3).

Drives run_health_monitor against a sqlite source_runs table with a fake
Telegram client, covering: stale-ingestion alert + cooldown dedup +
recovery, the provider-quota (rate_limited) alert, and the disabled switch.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.models.source import SourceRun
from catalyst_radar.repositories.config_repository import ConfigRepository
from catalyst_radar.repositories.notification_repository import TelegramChatRepository
from catalyst_radar.services import monitoring

NOW = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)


@dataclass
class _SendResult:
    ok: bool = True
    message_id: int | None = 1
    error_code: int | None = None
    description: str | None = None


class _FakeClient:
    """Stand-in for TelegramClient: always configured, records sends."""

    configured = True

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_message(self, chat_id: str, text: str, **_kw: object) -> _SendResult:
        self.sent.append((chat_id, text))
        return _SendResult()


async def _add_run(
    session: AsyncSession,
    *,
    source_name: str,
    status: str,
    finished_at: datetime | None,
    started_at: datetime | None = None,
    last_error: str | None = None,
) -> None:
    session.add(
        SourceRun(
            source_name=source_name,
            status=status,
            started_at=started_at or finished_at or NOW,
            finished_at=finished_at,
            last_error=last_error,
        )
    )
    await session.commit()


async def _success(session: AsyncSession, hours_old: float) -> None:
    await _add_run(
        session,
        source_name="eodhd.earnings",
        status="success",
        finished_at=NOW - timedelta(hours=hours_old),
    )


@pytest.mark.asyncio
async def test_fresh_ingestion_no_alert(db_session: AsyncSession) -> None:
    await _success(db_session, hours_old=2)
    await TelegramChatRepository(db_session).register("123")
    client = _FakeClient()
    summary = await monitoring.run_health_monitor(db_session, client, now=NOW)
    assert not summary.stale_alert
    assert summary.sent == 0
    assert client.sent == []


@pytest.mark.asyncio
async def test_stale_ingestion_alerts_then_cools_down(db_session: AsyncSession) -> None:
    # Newest success is 20h old; threshold is 12h → stale.
    await _success(db_session, hours_old=20)
    await TelegramChatRepository(db_session).register("123")
    client = _FakeClient()

    first = await monitoring.run_health_monitor(db_session, client, now=NOW)
    assert first.stale_alert and first.sent == 1
    assert "ingestion stalled" in client.sent[0][1]

    # Within the 6h cooldown → no second ping.
    client.sent.clear()
    second = await monitoring.run_health_monitor(
        db_session, client, now=NOW + timedelta(hours=1)
    )
    assert not second.stale_alert and client.sent == []

    # Past the cooldown, still stale → re-alert.
    third = await monitoring.run_health_monitor(
        db_session, client, now=NOW + timedelta(hours=7)
    )
    assert third.stale_alert and third.sent == 1


@pytest.mark.asyncio
async def test_recovery_note_after_stall(db_session: AsyncSession) -> None:
    await _success(db_session, hours_old=20)
    await TelegramChatRepository(db_session).register("123")
    client = _FakeClient()
    await monitoring.run_health_monitor(db_session, client, now=NOW)  # alert + set marker

    # A fresh success lands; the next tick should send the recovery note + clear.
    await _add_run(
        db_session,
        source_name="eodhd.earnings",
        status="success",
        finished_at=NOW + timedelta(hours=1),
    )
    client.sent.clear()
    summary = await monitoring.run_health_monitor(
        db_session, client, now=NOW + timedelta(hours=1, minutes=5)
    )
    assert summary.stale_recovered and summary.sent == 1
    assert "resumed" in client.sent[0][1]
    # Marker cleared.
    assert not await ConfigRepository(db_session).get(monitoring._INGEST_MARKER)


@pytest.mark.asyncio
async def test_no_success_ever_alerts(db_session: AsyncSession) -> None:
    # Only failures on record → treated as stalled.
    await _add_run(
        db_session,
        source_name="eodhd.earnings",
        status="failed",
        finished_at=NOW - timedelta(hours=1),
    )
    await TelegramChatRepository(db_session).register("123")
    client = _FakeClient()
    summary = await monitoring.run_health_monitor(db_session, client, now=NOW)
    assert summary.stale_alert and summary.sent == 1


@pytest.mark.asyncio
async def test_latest_success_ignores_null_finished_at(db_session: AsyncSession) -> None:
    from catalyst_radar.repositories.source_repository import SourceRunRepository

    # A success row that never recorded a finish timestamp. Postgres sorts
    # NULLs first under ORDER BY DESC, so this must not masquerade as latest.
    await _add_run(
        db_session,
        source_name="eodhd.earnings",
        status="success",
        finished_at=None,
        started_at=NOW - timedelta(minutes=5),
    )
    repo = SourceRunRepository(db_session)
    assert await repo.latest_success() is None  # null finished excluded
    assert await repo.has_any_success() is True  # but history exists

    # A properly finished success is what wins.
    await _add_run(
        db_session,
        source_name="eodhd.earnings",
        status="success",
        finished_at=NOW - timedelta(minutes=2),
    )
    latest = await repo.latest_success()
    assert latest is not None and latest.finished_at is not None


@pytest.mark.asyncio
async def test_transient_none_with_history_no_false_alarm(
    db_session: AsyncSession, monkeypatch: object
) -> None:
    """If latest_success() comes back empty on a system that has succeeded
    before (a tick racing a worker/DB restart), the monitor must NOT fire the
    'never succeeded' alarm."""
    from catalyst_radar.repositories.source_repository import SourceRunRepository

    await _success(db_session, hours_old=1)  # real, healthy success on record
    await TelegramChatRepository(db_session).register("123")

    async def _none(_self: SourceRunRepository) -> None:
        return None

    monkeypatch.setattr(SourceRunRepository, "latest_success", _none)
    client = _FakeClient()
    summary = await monitoring.run_health_monitor(db_session, client, now=NOW)
    assert not summary.stale_alert
    assert client.sent == []


@pytest.mark.asyncio
async def test_recent_blip_with_success_within_stale_window_no_stall(
    db_session: AsyncSession,
) -> None:
    """A slow (6h-cadence) source that had ONE transient failure but succeeded
    within the per-source stale window must NOT be flagged stalled — the exact
    akshare.stock_ipo_review_em false alarm: a single ChunkedEncodingError plus
    a missed run pushed the last success just past the 12h gap, yet the source
    is healthy and self-recovers on its next tick."""
    # Another source keeps the global pipeline fresh (isolate per-source logic).
    await _success(db_session, hours_old=1)
    # Flaky source: last success 14h ago (within the 18h stale window) + a
    # transient failure 1h ago. Under the old "no success in the 12h gap
    # window" rule this would alert; cadence-aware logic tolerates it.
    await _add_run(
        db_session,
        source_name="akshare.stock_ipo_review_em",
        status="success",
        finished_at=NOW - timedelta(hours=14),
    )
    await _add_run(
        db_session,
        source_name="akshare.stock_ipo_review_em",
        status="empty",
        finished_at=NOW - timedelta(hours=1),
        last_error="http 0",
    )
    await TelegramChatRepository(db_session).register("123")
    client = _FakeClient()
    summary = await monitoring.run_health_monitor(db_session, client, now=NOW)
    assert not summary.stale_alert
    assert "ipo_review" not in "".join(t for _c, t in client.sent)


@pytest.mark.asyncio
async def test_slow_source_still_stalls_past_stale_window(db_session: AsyncSession) -> None:
    """The relaxed rule must still catch a genuinely dead slow source: last
    success beyond the per-source stale window (18h) with a recent failure."""
    await _success(db_session, hours_old=1)  # keep the global pipeline fresh
    await _add_run(
        db_session,
        source_name="akshare.stock_ipo_review_em",
        status="success",
        finished_at=NOW - timedelta(hours=20),
    )
    await _add_run(
        db_session,
        source_name="akshare.stock_ipo_review_em",
        status="empty",
        finished_at=NOW - timedelta(hours=1),
        last_error="http 0",
    )
    await TelegramChatRepository(db_session).register("123")
    client = _FakeClient()
    summary = await monitoring.run_health_monitor(db_session, client, now=NOW)
    assert summary.stale_alert and summary.sent == 1
    assert "akshare.stock_ipo_review_em" in client.sent[0][1]


@pytest.mark.asyncio
async def test_quota_alert_on_rate_limited(db_session: AsyncSession) -> None:
    # Fresh success keeps ingestion healthy; a recent rate_limited run trips quota.
    await _success(db_session, hours_old=1)
    await _add_run(
        db_session,
        source_name="eodhd.company_reference.US",
        status="rate_limited",
        finished_at=NOW - timedelta(minutes=30),
        started_at=NOW - timedelta(minutes=31),
        last_error="EODHD daily API quota exhausted (HTTP 402).",
    )
    await TelegramChatRepository(db_session).register("123")
    client = _FakeClient()
    summary = await monitoring.run_health_monitor(db_session, client, now=NOW)
    assert summary.quota_alert and not summary.stale_alert
    assert any("quota" in text for _cid, text in client.sent)


@pytest.mark.asyncio
async def test_disabled_switch_skips(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from catalyst_radar.config import settings

    monkeypatch.setattr(settings, "monitor_self_enabled", False)
    await TelegramChatRepository(db_session).register("123")
    client = _FakeClient()
    summary = await monitoring.run_health_monitor(db_session, client, now=NOW)
    assert summary.skipped == "disabled" and summary.sent == 0


@pytest.mark.asyncio
async def test_per_source_stall_not_masked_by_fast_source(db_session: AsyncSession) -> None:
    """Fix for the masking bug: a fresh success from one fast source must NOT
    hide another source that ran in-window but never succeeded."""
    # Fast, healthy source 10 min ago keeps the global latest_success fresh.
    await _add_run(
        db_session, source_name="cn_ipo", status="success", finished_at=NOW - timedelta(minutes=10)
    )
    # A different source ran 1h ago and failed — it is genuinely stalled.
    await _add_run(
        db_session,
        source_name="eodhd.news",
        status="failed",
        finished_at=NOW - timedelta(hours=1),
        last_error="boom",
    )
    await TelegramChatRepository(db_session).register("123")
    client = _FakeClient()
    summary = await monitoring.run_health_monitor(db_session, client, now=NOW)
    assert summary.stale_alert and summary.sent == 1
    assert "eodhd.news" in client.sent[0][1]


@pytest.mark.asyncio
async def test_quota_detected_via_http_402_last_error(db_session: AsyncSession) -> None:
    """The busy EODHD paths record status='failed' + 'http 402' (only
    company_sync sets 'rate_limited'); the watchdog must still flag quota."""
    await _success(db_session, hours_old=1)  # keep ingestion healthy
    await _add_run(
        db_session,
        source_name="eodhd.catalyst.AAPL",
        status="failed",
        finished_at=NOW - timedelta(minutes=20),
        last_error="http 402",
    )
    await TelegramChatRepository(db_session).register("123")
    client = _FakeClient()
    summary = await monitoring.run_health_monitor(db_session, client, now=NOW)
    assert summary.quota_alert and not summary.stale_alert
    assert "eodhd.catalyst.AAPL" in "".join(t for _c, t in client.sent)


@pytest.mark.asyncio
async def test_no_quota_alert_after_source_recovered(db_session: AsyncSession) -> None:
    """A 402 followed by a later success for the SAME source must not alert —
    the latest run is what matters, not any historical rate-limit."""
    await _add_run(
        db_session,
        source_name="eodhd.catalyst.AAPL",
        status="failed",
        finished_at=NOW - timedelta(hours=2),
        last_error="http 402",
    )
    await _add_run(
        db_session,
        source_name="eodhd.catalyst.AAPL",
        status="success",
        finished_at=NOW - timedelta(minutes=10),
    )
    await TelegramChatRepository(db_session).register("123")
    client = _FakeClient()
    summary = await monitoring.run_health_monitor(db_session, client, now=NOW)
    assert not summary.quota_alert


async def _add_failures(
    session: AsyncSession,
    *,
    source_name: str,
    count: int,
    first_hours_old: float,
    spacing_hours: float = 1.0,
) -> None:
    """Seed ``count`` failed runs for one source, the oldest ``first_hours_old``
    hours back and the rest more recent at ``spacing_hours`` intervals."""
    for i in range(count):
        age = first_hours_old - i * spacing_hours
        await _add_run(
            session,
            source_name=source_name,
            status="failed",
            finished_at=NOW - timedelta(hours=age),
            last_error="ChunkedEncodingError('Response ended prematurely')",
        )


async def test_failure_streak_alerts_instability(db_session: AsyncSession) -> None:
    """A source failing repeatedly within the 24h streak window — but with a
    fresh success inside the 12h gap window so it isn't 'stalled' — fires the
    earlier instability tier."""
    # Keep the global pipeline healthy.
    await _success(db_session, hours_old=1)
    # 4 failures spread across the last ~20h, plus a recent success so the
    # source is NOT counted as stalled in the 12h window.
    await _add_failures(db_session, source_name="eastmoney_news", count=4, first_hours_old=20)
    await _add_run(
        db_session,
        source_name="eastmoney_news",
        status="success",
        finished_at=NOW - timedelta(minutes=30),
    )
    await TelegramChatRepository(db_session).register("123")
    client = _FakeClient()
    summary = await monitoring.run_health_monitor(db_session, client, now=NOW)
    assert summary.instability_alert and not summary.stale_alert
    text = "".join(t for _c, t in client.sent)
    assert "instability" in text.lower()
    assert "eastmoney_news" in text


async def test_failure_streak_below_threshold_no_alert(db_session: AsyncSession) -> None:
    await _success(db_session, hours_old=1)
    # Only 3 failures — below the default threshold of 4.
    await _add_failures(db_session, source_name="eastmoney_news", count=3, first_hours_old=20)
    await _add_run(
        db_session,
        source_name="eastmoney_news",
        status="success",
        finished_at=NOW - timedelta(minutes=30),
    )
    await TelegramChatRepository(db_session).register("123")
    client = _FakeClient()
    summary = await monitoring.run_health_monitor(db_session, client, now=NOW)
    assert not summary.instability_alert


async def test_stalled_source_not_double_reported_as_instability(
    db_session: AsyncSession,
) -> None:
    """A source already flagged as stalled (no success in the gap window) must
    not ALSO be reported under the instability tier."""
    await _success(db_session, hours_old=1)  # another source keeps global fresh
    # 4 failures, all recent, NO success → this source is 'stalled'.
    await _add_failures(
        db_session, source_name="eodhd.news", count=4, first_hours_old=5, spacing_hours=1
    )
    await TelegramChatRepository(db_session).register("123")
    client = _FakeClient()
    summary = await monitoring.run_health_monitor(db_session, client, now=NOW)
    assert summary.stale_alert
    assert not summary.instability_alert
    text = "".join(t for _c, t in client.sent)
    assert "eodhd.news" in text
    # The stale alert mentions it; the instability tier must not duplicate.
    assert text.lower().count("instability") == 0


async def test_instability_cooldown_suppresses_realert(db_session: AsyncSession) -> None:
    await _success(db_session, hours_old=1)
    await _add_failures(db_session, source_name="eastmoney_news", count=4, first_hours_old=20)
    await _add_run(
        db_session,
        source_name="eastmoney_news",
        status="success",
        finished_at=NOW - timedelta(minutes=30),
    )
    await TelegramChatRepository(db_session).register("123")
    client = _FakeClient()

    first = await monitoring.run_health_monitor(db_session, client, now=NOW)
    assert first.instability_alert

    client.sent.clear()
    second = await monitoring.run_health_monitor(
        db_session, client, now=NOW + timedelta(hours=1)
    )
    assert not second.instability_alert and client.sent == []


@pytest.mark.asyncio
async def test_marker_not_set_when_delivery_fails(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the alert can't be delivered (Telegram muted), the cooldown marker
    must NOT be armed — otherwise the real alert is suppressed once delivery
    is restored."""
    from catalyst_radar.config import settings

    await _success(db_session, hours_old=20)  # stalled
    await TelegramChatRepository(db_session).register("123")
    client = _FakeClient()

    monkeypatch.setattr(settings, "telegram_alerts_enabled", False)
    first = await monitoring.run_health_monitor(db_session, client, now=NOW)
    assert not first.stale_alert and first.sent == 0
    assert not await ConfigRepository(db_session).get(monitoring._INGEST_MARKER)

    # Telegram restored a minute later → the stall alert finally fires.
    monkeypatch.setattr(settings, "telegram_alerts_enabled", True)
    second = await monitoring.run_health_monitor(
        db_session, client, now=NOW + timedelta(minutes=1)
    )
    assert second.stale_alert and second.sent == 1


@pytest.mark.asyncio
async def test_unconfigured_key_skips_never_alert(db_session: AsyncSession) -> None:
    """A self-hosted install without an EODHD key records its runs as
    "skipped" every tick; that is an opt-out, not a stall or instability."""
    await _success(db_session, hours_old=1)  # a keyless source is healthy
    for i in range(6):
        await _add_run(
            db_session,
            source_name="eodhd.ipos",
            status="skipped",
            finished_at=NOW - timedelta(hours=i + 0.5),
            last_error="EODHD_API_KEY is not configured",
        )
    await TelegramChatRepository(db_session).register("123")
    client = _FakeClient()
    summary = await monitoring.run_health_monitor(db_session, client, now=NOW)
    assert not summary.stale_alert
    assert not summary.instability_alert
    assert client.sent == []
