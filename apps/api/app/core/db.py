"""Async SQLAlchemy engine and session factory — the only place the API opens a Postgres connection pool."""

import uuid
from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings

settings = get_settings()

# One pooled engine per process; pool size is what scales, not process count (see ARCHITECTURE.md #11)
# `app_database_url`, when set, is a distinct low-privilege role RLS actually applies to — the
# table owner (`database_url`) is exempt from row-level security by Postgres default.
engine = create_async_engine(
    settings.app_database_url or settings.database_url, echo=settings.debug, pool_pre_ping=True
)

async_session_factory = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    """Shared declarative base every ORM model inherits from."""


async def set_workspace_scope(db: AsyncSession, workspace_id: uuid.UUID) -> None:
    """Set `app.workspace_id` for the rest of the current transaction — what row-level security
    policies check on every tenant-scoped table (see the RLS migration).

    `set_config(..., true)` is the parameterized equivalent of `SET LOCAL`: transaction-scoped,
    so it can't leak into the next request when this connection goes back to the pool, and it
    resets automatically at the commit `get_db()` issues when the request finishes.
    """
    await db.execute(
        text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
        {"workspace_id": str(workspace_id)},
    )


async def get_db() -> AsyncGenerator[AsyncSession]:
    """Yield a request-scoped session, committing on success and rolling back on error."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
