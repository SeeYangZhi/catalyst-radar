from fastapi import APIRouter
from redis import asyncio as aioredis
from sqlalchemy import text

from catalyst_radar.api.deps import SessionDep
from catalyst_radar.config import settings

router = APIRouter()


async def _ping_redis() -> bool:
    """Redis is the Celery broker; if it's down, every background task
    (alerts, enrichment, polling) silently queues up against a dead
    backend. Healthcheck should reflect that."""
    try:
        client = aioredis.from_url(settings.redis_url, socket_timeout=2.0)
        try:
            await client.ping()
        finally:
            await client.aclose()
        return True
    except Exception:  # noqa: BLE001 - any failure is "redis not reachable"
        return False


@router.get("/health")
async def health(session: SessionDep) -> dict[str, object]:
    db_ok = True
    try:
        await session.execute(text("SELECT 1"))
    except Exception:
        db_ok = False

    redis_ok = await _ping_redis()

    return {
        "status": "ok" if (db_ok and redis_ok) else "degraded",
        "app": settings.app_name,
        "environment": settings.environment,
        "database": "ok" if db_ok else "unreachable",
        "redis": "ok" if redis_ok else "unreachable",
    }
