"""Async SQLAlchemy engine and session factory — the only place the API opens a Postgres connection pool."""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings

settings = get_settings()

# One pooled engine per process; pool size is what scales, not process count (see ARCHITECTURE.md #11)
engine = create_async_engine(settings.database_url, echo=settings.debug, pool_pre_ping=True)

async_session_factory = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    """Shared declarative base every ORM model inherits from."""


async def get_db() -> AsyncGenerator[AsyncSession]:
    """Yield a request-scoped session, committing on success and rolling back on error."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
