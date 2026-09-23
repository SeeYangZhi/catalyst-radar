from fastapi import APIRouter

from catalyst_radar.api.v1 import (
    auth,
    classifier_runs,
    companies,
    dashboard,
    entities,
    events,
    health,
    me,
    notifications,
    settings,
    source_runs,
    telegram,
)

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(me.router, tags=["me"])
api_router.include_router(settings.router, tags=["settings"])
api_router.include_router(companies.router)
api_router.include_router(events.router)
api_router.include_router(telegram.router)
api_router.include_router(notifications.router)
api_router.include_router(source_runs.router)
api_router.include_router(classifier_runs.router)
api_router.include_router(dashboard.router)
api_router.include_router(entities.router)
