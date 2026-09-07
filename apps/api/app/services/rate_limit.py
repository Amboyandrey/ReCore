"""A sliding-window rate limiter — one atomic Redis script shared by every limited endpoint."""

import time

from redis.asyncio import Redis

# Evicts entries older than the window, counts what's left, and admits the request only if under
# the limit — all as one atomic script, so concurrent requests can't race past the limit.
_SLIDING_WINDOW_SCRIPT = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window_ms = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
redis.call('ZREMRANGEBYSCORE', key, 0, now - window_ms)
local count = redis.call('ZCARD', key)
if count < limit then
    redis.call('ZADD', key, now, now .. '-' .. math.random())
    redis.call('PEXPIRE', key, window_ms)
    return 1
else
    return 0
end
"""


async def check_rate_limit(redis: Redis, key: str, *, limit: int, window_seconds: int) -> bool:
    """Record one attempt under `key` and report whether it's within the allowed rate."""
    now_ms = int(time.time() * 1000)
    window_ms = window_seconds * 1000
    allowed = await redis.eval(_SLIDING_WINDOW_SCRIPT, 1, key, now_ms, window_ms, limit)
    return bool(allowed)
