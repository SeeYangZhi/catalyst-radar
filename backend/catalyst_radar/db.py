from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from catalyst_radar.config import settings

engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_timeout=settings.db_pool_timeout,
    pool_pre_ping=True,
    # asyncpg caches prepared-statement plans per pooled connection. After
    # a migration alters column types, long-lived pooled connections raise
    # "cached plan must not change result type". Disabling the statement
    # cache keeps pooled connections correct across schema changes.
    connect_args={"statement_cache_size": 0},
)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_session() -> AsyncGenerator[AsyncSession]:
    """FastAPI dependency yielding an AsyncSession."""
    async with async_session_factory() as session:
        yield session
