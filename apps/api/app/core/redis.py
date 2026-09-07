"""Async Redis client — the single connection pool backing sessions, rate limits, streams and caches."""

from collections.abc import AsyncGenerator

from redis.asyncio import ConnectionPool, Redis

from app.core.config import get_settings

settings = get_settings()

# One connection pool per process, reused across every Redis-backed feature (see ARCHITECTURE.md #7)
_pool = ConnectionPool.from_url(settings.redis_url, decode_responses=True)


async def get_redis() -> AsyncGenerator[Redis]:
    """Yield a Redis client bound to the shared connection pool for the duration of a request."""
    client = Redis(connection_pool=_pool)
    try:
        yield client
    finally:
        await client.aclose()


def new_redis_client() -> Redis:
    """Return a Redis client bound to the shared pool — for code that isn't a request dependency,
    like a background generation task that outlives the request that started it."""
    return Redis(connection_pool=_pool)
