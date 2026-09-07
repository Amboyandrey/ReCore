"""The sliding-window limiter admits up to the limit, then blocks, then recovers as time passes."""

import asyncio

from redis.asyncio import Redis

from app.services.rate_limit import check_rate_limit


async def test_allows_up_to_the_limit_then_blocks(redis_client: Redis) -> None:
    """The third attempt in a limit-of-two window is refused."""
    key = "rl:test:limit"
    assert await check_rate_limit(redis_client, key, limit=2, window_seconds=60) is True
    assert await check_rate_limit(redis_client, key, limit=2, window_seconds=60) is True
    assert await check_rate_limit(redis_client, key, limit=2, window_seconds=60) is False


async def test_recovers_once_the_window_passes(redis_client: Redis) -> None:
    """After the window elapses, the same key is allowed again."""
    key = "rl:test:recovery"
    assert await check_rate_limit(redis_client, key, limit=1, window_seconds=1) is True
    assert await check_rate_limit(redis_client, key, limit=1, window_seconds=1) is False

    await asyncio.sleep(1.1)

    assert await check_rate_limit(redis_client, key, limit=1, window_seconds=1) is True


async def test_different_keys_are_independent(redis_client: Redis) -> None:
    """One key being at its limit doesn't affect another key's budget."""
    assert await check_rate_limit(redis_client, "rl:test:a", limit=1, window_seconds=60) is True
    assert await check_rate_limit(redis_client, "rl:test:a", limit=1, window_seconds=60) is False
    assert await check_rate_limit(redis_client, "rl:test:b", limit=1, window_seconds=60) is True
