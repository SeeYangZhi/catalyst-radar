"""Optional Sentry error tracking (deep-analysis task 3).

Failures currently surface only by reading container logs by hand. Wiring
Sentry makes uncaught exceptions (API + Celery tasks) page somewhere. Kept
fully optional: a no-op unless a DSN is configured AND sentry-sdk is
installed (the ``monitoring`` extra), so the base image stays lean and the
feature is one env var away.
"""

from catalyst_radar.config import settings
from catalyst_radar.logging import get_logger

log = get_logger(__name__)

_initialised = False


def init_error_tracking() -> bool:
    """Initialise Sentry if configured. Idempotent; returns True when active.

    Safe to call from both the FastAPI lifespan and the Celery worker boot —
    each process initialises its own SDK once."""
    global _initialised
    if _initialised or not settings.sentry_dsn:
        return _initialised
    try:
        import sentry_sdk
    except ImportError:
        log.warning(
            "sentry_dsn_set_but_sdk_missing",
            hint="install the 'monitoring' extra: uv sync --extra monitoring",
        )
        return False
    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.environment,
        traces_sample_rate=settings.sentry_traces_sample_rate,
    )
    _initialised = True
    log.info("sentry_initialised", environment=settings.environment)
    return True
