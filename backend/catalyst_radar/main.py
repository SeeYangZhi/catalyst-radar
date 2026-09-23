from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from catalyst_radar.api.v1 import api_router
from catalyst_radar.config import settings
from catalyst_radar.logging import configure_logging, get_logger
from catalyst_radar.observability import init_error_tracking
from catalyst_radar.seed import seed_default_admin

log = get_logger(__name__)


async def _sync_telegram_webhook() -> None:
    """Reconcile Telegram to the configured mode on startup: register the
    webhook when TELEGRAM_WEBHOOK_URL is set, otherwise clear any stale
    webhook so getUpdates polling can run without a 409 conflict."""
    from catalyst_radar.services.telegram_client import TelegramClient

    client = TelegramClient()
    if not client.configured:
        return
    if settings.telegram_webhook_url:
        ok = await client.set_webhook(
            settings.telegram_webhook_url, settings.telegram_webhook_secret
        )
        log.info("telegram_webhook_registered", url=settings.telegram_webhook_url, ok=ok)
    else:
        await client.delete_webhook()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    init_error_tracking()
    log.info("startup", app=settings.app_name, environment=settings.environment)
    problems = settings.production_secret_problems()
    if problems:
        for problem in problems:
            log.error("insecure_production_default", problem=problem)
        raise RuntimeError(
            "Refusing to start in production with insecure defaults: " + "; ".join(problems)
        )
    try:
        await seed_default_admin()
    except Exception as exc:  # noqa: BLE001 - never block startup on seed
        log.warning("seed_admin_failed", error=str(exc))
    try:
        await _sync_telegram_webhook()
    except Exception as exc:  # noqa: BLE001 - never block startup on telegram
        log.warning("telegram_webhook_sync_failed", error=str(exc))
    yield
    log.info("shutdown")


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_origin_regex=settings.cors_origin_regex,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix="/api/v1")


@app.get("/")
async def root() -> dict[str, str]:
    return {"app": settings.app_name, "docs": "/docs", "health": "/api/v1/health"}
