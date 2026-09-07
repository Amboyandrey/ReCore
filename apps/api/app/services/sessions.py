"""Redis-backed sessions — the opaque cookie value is a key into this store, never a JWT."""

import uuid
from dataclasses import dataclass

from redis.asyncio import Redis

from app.core.config import get_settings
from app.core.security import generate_token

settings = get_settings()


def _session_key(session_id: str) -> str:
    """Build the Redis key a session's data is stored under."""
    return f"session:{session_id}"


@dataclass(frozen=True)
class SessionData:
    """What a session remembers: which user it belongs to, and the CSRF token paired with it."""

    user_id: uuid.UUID
    csrf_token: str


async def create_session(redis: Redis, user_id: uuid.UUID) -> tuple[str, str]:
    """Start a new session for a user and return its (session_id, csrf_token) pair."""
    session_id = generate_token()
    csrf_token = generate_token()
    await redis.hset(
        _session_key(session_id), mapping={"user_id": str(user_id), "csrf_token": csrf_token}
    )
    await redis.expire(_session_key(session_id), settings.session_ttl_seconds)
    return session_id, csrf_token


async def get_session(redis: Redis, session_id: str) -> SessionData | None:
    """Look up a session and slide its expiry forward, or return None if it doesn't exist."""
    data = await redis.hgetall(_session_key(session_id))
    if not data:
        return None
    await redis.expire(_session_key(session_id), settings.session_ttl_seconds)
    # The client is configured with decode_responses=True, so these are always str at runtime —
    # redis-py's stubs just can't express that statically.
    return SessionData(user_id=uuid.UUID(str(data["user_id"])), csrf_token=str(data["csrf_token"]))


async def delete_session(redis: Redis, session_id: str) -> None:
    """End a session immediately — the cookie that named it becomes worthless on the next request."""
    await redis.delete(_session_key(session_id))
