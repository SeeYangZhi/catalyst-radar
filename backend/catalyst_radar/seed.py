from catalyst_radar.config import settings
from catalyst_radar.db import async_session_factory
from catalyst_radar.logging import get_logger
from catalyst_radar.models.user import User
from catalyst_radar.repositories.user_repository import UserRepository
from catalyst_radar.security import hash_password

log = get_logger(__name__)


async def seed_default_admin() -> None:
    """Create the default admin if no users exist (TIS single-user pattern)."""
    async with async_session_factory() as session:
        repo = UserRepository(session)
        if await repo.count() > 0:
            log.info("seed_admin_skipped", reason="users_exist")
            return

        admin = User(
            email=settings.default_admin_email.strip().lower(),
            hashed_password=hash_password(settings.default_admin_password),
            full_name="Catalyst Radar Admin",
            role="admin",
            is_active=True,
        )
        await repo.create(admin)
        log.info("seed_admin_created", email=admin.email)
