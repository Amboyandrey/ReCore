"""Shared pytest fixtures — a migrated test database, an isolated session per test, and a test client."""

import subprocess
import sys
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import app.core.redis as redis_module
from app.core.db import engine
from app.main import app

API_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session", autouse=True)
def apply_migrations() -> None:
    """Run Alembic against the test database once, before any test touches it."""
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=API_ROOT, check=True)


@pytest.fixture(autouse=True)
async def _reset_state_after_test() -> AsyncGenerator[None]:
    """Reset everything a test could have touched. `db_session` is isolated by its own rolled-
    back transaction, but a test that drives the app through `client` commits real rows via the
    app's own `get_db` — so tables get truncated here too. Connection pools are also dropped:
    pytest-asyncio gives every test its own event loop, but both pools are module-level
    singletons, so a connection left checked-in from this test's loop would otherwise be reused
    (and fail) in the next one."""
    yield
    redis_client = Redis(connection_pool=redis_module._pool)
    await redis_client.flushdb()
    await redis_client.aclose()
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE TABLE users, workspaces, workspace_members, invitations CASCADE"))
    await engine.dispose()
    await redis_module._pool.disconnect()


@pytest.fixture
async def redis_client() -> AsyncGenerator[Redis]:
    """Yield a Redis client bound to the app's own connection pool."""
    client = Redis(connection_pool=redis_module._pool)
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
async def db_session() -> AsyncGenerator[AsyncSession]:
    """Yield a session bound to a transaction that's rolled back after the test, for isolation."""
    async with engine.connect() as conn:
        trans = await conn.begin()
        # create_savepoint: a flush error inside the test rolls back to a savepoint, not the
        # outer transaction — otherwise SQLAlchemy ends the outer transaction itself on error,
        # and this fixture's own rollback below then warns that it's already deassociated.
        session = AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint")
        try:
            yield session
        finally:
            await session.close()
            await trans.rollback()


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient]:
    """Yield an httpx client that calls the FastAPI app in-process, with no real network hop."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
