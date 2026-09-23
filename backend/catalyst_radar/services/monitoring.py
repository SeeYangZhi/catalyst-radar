"""Self-monitoring watchdog (deep-analysis task 3).

An alerting product that doesn't alert on *itself* is the scariest gap: if
ingestion silently stalls, or EODHD's daily quota trips (HTTP 402), nothing
ships and the user only learns about it by noticing the silence — exactly
what an operator would otherwise catch only by hand. This beat task watches the
``source_runs`` table and pushes a Telegram alert when:

  - a source that ran recently keeps **failing** (stalled), or the whole
    pipeline has produced no success at all within the window, or
  - a source's latest run hit its provider **quota** (status
    ``rate_limited`` OR an HTTP 402 recorded in ``last_error`` — the busy
    EODHD calendar/news/IPO paths record a plain ``failed`` + ``http 402``,
    only company_sync sets ``rate_limited``).

Detection is **per source** so one fast healthy source can't mask another
being dead, and a recovered source (latest run is a success) stops alerting.
Alerts are cooldown-deduped via ``app_config`` markers — and the marker is
only set once a message is actually **delivered**, so a stall that happens
while Telegram is muted/unconfigured still fires the moment delivery is
possible. A one-line recovery note is sent when ingestion resumes.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.logging import get_logger
from catalyst_radar.models.base import as_utc, utcnow
from catalyst_radar.models.source import SourceRun
from catalyst_radar.repositories.config_repository import ConfigRepository
from catalyst_radar.repositories.notification_repository import TelegramChatRepository
from catalyst_radar.repositories.source_repository import SourceRunRepository
from catalyst_radar.runtime_config import effective
from catalyst_radar.services.telegram_client import TelegramClient

log = get_logger(__name__)

# app_config markers. Underscore-prefixed so they never collide with the
# user-editable runtime-config keys (validated against an allowlist). A
# truthy value = "currently in the alerted state, last pinged at <iso>".
_INGEST_MARKER = "_monitor_ingestion_alert_at"
_QUOTA_MARKER = "_monitor_quota_alert_at"
_INSTABILITY_MARKER = "_monitor_instability_alert_at"


@dataclass(slots=True)
class MonitorSummary:
    stale_alert: bool = False
    stale_recovered: bool = False
    quota_alert: bool = False
    instability_alert: bool = False
    sent: int = 0  # Telegram deliveries this run
    skipped: str | None = None


def _is_quota_run(run: SourceRun) -> bool:
    """Whether a run indicates a provider quota / rate-limit trip. Covers
    company_sync's explicit ``rate_limited`` status AND the busy EODHD paths
    that record a plain ``failed`` with ``http 402`` in ``last_error``."""
    if run.status == "rate_limited":
        return True
    err = (run.last_error or "").lower()
    return "402" in err or "quota" in err


def _cooldown_elapsed(marker: object, now: datetime, hours: int) -> bool:
    """True if we're allowed to (re-)alert: no active marker, or the last
    alert is older than the cooldown. Unparseable marker → allow."""
    if not marker:
        return True
    try:
        last = datetime.fromisoformat(str(marker))
    except (TypeError, ValueError):
        return True
    return (now - (as_utc(last) or now)) >= timedelta(hours=hours)


async def _alert(session: AsyncSession, client: TelegramClient, cfg, text: str) -> int:
    """Broadcast one alert to every active chat; returns the number actually
    delivered. Honours the global telegram_alerts_enabled switch and a
    missing bot token (both → 0, no marker should be set by the caller)."""
    if not bool(getattr(cfg, "telegram_alerts_enabled", True)):
        return 0
    if not client.configured:
        return 0
    sent = 0
    for chat in await TelegramChatRepository(session).admins():
        res = await client.send_message(chat.chat_id, text)
        if res.ok:
            sent += 1
        else:
            log.warning("monitor_alert_send_failed", chat_id=chat.chat_id, error=res.description)
    return sent


def _fmt_age(delta: timedelta) -> str:
    hours = delta.total_seconds() / 3600
    if hours < 1:
        return f"{int(delta.total_seconds() // 60)}m"
    if hours < 48:
        return f"{hours:.1f}h"
    return f"{hours / 24:.1f}d"


def _stale_text(
    stalled_sources: list[str],
    last_success: datetime | None,
    gap_hours: int,
    now: datetime,
    source_stale_hours: int,
) -> str:
    head = "⚠️ <b>Catalyst Radar: ingestion stalled</b>"
    if stalled_sources:
        shown = ", ".join(f"<code>{s}</code>" for s in stalled_sources[:6])
        extra = "" if len(stalled_sources) <= 6 else f" (+{len(stalled_sources) - 6} more)"
        return (
            f"{head}\nNo success in {source_stale_hours}h from: {shown}{extra}. "
            "Check the worker and provider connectivity."
        )
    if last_success is None:
        return f"{head}\nNo source run has ever succeeded. Check the worker + provider keys."
    age = _fmt_age(now - last_success)
    return (
        f"{head}\nNothing has succeeded in {age} (threshold {gap_hours}h). "
        "Check the Celery worker and provider connectivity."
    )


def _recovered_text() -> str:
    return "✅ <b>Catalyst Radar: ingestion resumed</b>\nA source run has succeeded again."


def _quota_text(quota_sources: list[str]) -> str:
    shown = ", ".join(f"<code>{s}</code>" for s in quota_sources[:6])
    return (
        "⚠️ <b>Catalyst Radar: provider quota hit</b>\n"
        f"Quota/rate-limit on: {shown}. Ingestion for these will recover when "
        "the quota resets."
    )


def _instability_text(streaks: list[tuple[str, int]], window_hours: int) -> str:
    """Earlier tier than the stall alert: sources that keep failing but
    recover before the stale threshold, so the stall guard never sees them."""
    shown = ", ".join(f"<code>{s}</code> ({n}×)" for s, n in streaks[:6])
    extra = "" if len(streaks) <= 6 else f" (+{len(streaks) - 6} more)"
    return (
        "⚠️ <b>Catalyst Radar: source instability</b>\n"
        f"Repeated failures in {window_hours}h: {shown}{extra}. "
        "Still recovering, but worth a look before it stalls outright."
    )


async def run_health_monitor(
    session: AsyncSession,
    client: TelegramClient | None = None,
    *,
    now: datetime | None = None,
) -> MonitorSummary:
    """One watchdog tick. Idempotent and cooldown-deduped, so it's safe to
    run on a tight beat schedule."""
    cfg = await effective(session)
    if not bool(getattr(cfg, "monitor_self_enabled", True)):
        return MonitorSummary(skipped="disabled")

    now = as_utc(now) or utcnow()
    client = client or TelegramClient()
    config_repo = ConfigRepository(session)
    runs = SourceRunRepository(session)
    summary = MonitorSummary()

    gap_hours = int(getattr(cfg, "monitor_max_ingestion_gap_hours", 12))
    source_stale_hours = max(
        gap_hours, int(getattr(cfg, "monitor_source_stale_hours", 18))
    )
    cooldown_hours = int(getattr(cfg, "monitor_alert_cooldown_hours", 6))
    streak_window_hours = int(getattr(cfg, "monitor_failure_streak_window_hours", 24))
    streak_threshold = int(getattr(cfg, "monitor_failure_streak_threshold", 4))

    # One query drives both checks: every run started within the gap window,
    # grouped by source (newest first, since runs_since is started_at DESC).
    recent = await runs.runs_since(now - timedelta(hours=gap_hours))
    by_source: dict[str, list[SourceRun]] = {}
    for run in recent:
        # "skipped" = the source's API key isn't configured: an intentional
        # opt-out on a self-hosted install, never a stall or a failure.
        if run.status == "skipped":
            continue
        by_source.setdefault(run.source_name, []).append(run)

    # A source whose LATEST run is a quota trip (and hasn't since recovered).
    quota_sources = sorted(name for name, rs in by_source.items() if _is_quota_run(rs[0]))
    # A source that ran in the gap window but hasn't SUCCEEDED within the
    # wider per-source stale window — and isn't merely quota-limited (reported
    # separately, not a worker "stall"). Keying the success check on
    # source_stale_hours (not the 12h gap) is what stops a slow 6h-cadence
    # source from false-alarming on a single transient blip: it succeeds
    # roughly every cycle, so it stays out of stalled_sources until it's been
    # genuinely dark for source_stale_hours.
    recovered_recently = await runs.success_sources_since(
        now - timedelta(hours=source_stale_hours)
    )
    stalled_sources = sorted(
        name
        for name, rs in by_source.items()
        if name not in recovered_recently and not _is_quota_run(rs[0])
    )

    # ── Ingestion freshness ──────────────────────────────────────────────
    latest = await runs.latest_success()
    last_success = as_utc(latest.finished_at) if latest else None
    # A None here on a system that has succeeded before is a transient read
    # anomaly (e.g. a monitor tick racing a worker/DB restart), not a real
    # cold start — don't let it fire the "never succeeded" alarm.
    if last_success is None and await runs.has_any_success():
        log.warning("monitor_latest_success_none_despite_history")
        global_stale = False
    else:
        # Global guard catches total worker death (no recent runs at all, so
        # stalled_sources is empty but the last success has aged out), and a
        # genuine cold start (no success ever → last_success is None).
        global_stale = (
            last_success is None or (now - last_success) > timedelta(hours=gap_hours)
        )
    stale = bool(stalled_sources) or global_stale
    ingest_marker = await config_repo.get(_INGEST_MARKER)
    if stale:
        if _cooldown_elapsed(ingest_marker, now, cooldown_hours):
            sent = await _alert(
                session,
                client,
                cfg,
                _stale_text(stalled_sources, last_success, gap_hours, now, source_stale_hours),
            )
            summary.sent += sent
            if sent:  # only arm the cooldown once the user was actually told
                await config_repo.set(_INGEST_MARKER, now.isoformat())
                summary.stale_alert = True
    elif ingest_marker:  # was alerting, now healthy → recovery note + clear
        sent = await _alert(session, client, cfg, _recovered_text())
        summary.sent += sent
        if sent:
            await config_repo.set(_INGEST_MARKER, "")
            summary.stale_recovered = True

    # ── Provider quota (EODHD 402) ───────────────────────────────────────
    quota_marker = await config_repo.get(_QUOTA_MARKER)
    if quota_sources:
        if _cooldown_elapsed(quota_marker, now, cooldown_hours):
            sent = await _alert(session, client, cfg, _quota_text(quota_sources))
            summary.sent += sent
            if sent:
                await config_repo.set(_QUOTA_MARKER, now.isoformat())
                summary.quota_alert = True
    elif quota_marker:  # quota cleared → reset so the next trip re-alerts
        await config_repo.set(_QUOTA_MARKER, "")

    # ── Failure streaks (early instability tier) ─────────────────────────
    # A source can keep failing yet recover before the stale threshold, so it
    # never trips the stall guard. Count failures over the (wider) streak
    # window and flag any source at/above the threshold that isn't already
    # being reported as stalled or quota-limited.
    failed_counts = await runs.failed_counts_since(now - timedelta(hours=streak_window_hours))
    already_reported = set(stalled_sources) | set(quota_sources)
    streaks = sorted(
        (
            (name, count)
            for name, count in failed_counts.items()
            if count >= streak_threshold and name not in already_reported
        ),
        key=lambda nc: (-nc[1], nc[0]),
    )
    instability_marker = await config_repo.get(_INSTABILITY_MARKER)
    if streaks:
        if _cooldown_elapsed(instability_marker, now, cooldown_hours):
            sent = await _alert(
                session, client, cfg, _instability_text(streaks, streak_window_hours)
            )
            summary.sent += sent
            if sent:
                await config_repo.set(_INSTABILITY_MARKER, now.isoformat())
                summary.instability_alert = True
    elif instability_marker:  # streaks cleared → reset so the next flap re-alerts
        await config_repo.set(_INSTABILITY_MARKER, "")

    return summary
