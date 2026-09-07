"""Sessions round-trip through Redis and disappear the moment they're deleted."""

import uuid

from redis.asyncio import Redis

from app.services.sessions import create_session, delete_session, get_session


async def test_session_round_trips(redis_client: Redis) -> None:
    """A created session comes back with the same user id and csrf token."""
    user_id = uuid.uuid4()
    session_id, csrf_token = await create_session(redis_client, user_id)

    session = await get_session(redis_client, session_id)

    assert session is not None
    assert session.user_id == user_id
    assert session.csrf_token == csrf_token


async def test_unknown_session_returns_none(redis_client: Redis) -> None:
    """Looking up a session id that was never issued finds nothing."""
    assert await get_session(redis_client, "not-a-real-session") is None


async def test_deleted_session_is_gone(redis_client: Redis) -> None:
    """Once deleted, a session id no longer resolves — logout is instant revocation."""
    session_id, _ = await create_session(redis_client, uuid.uuid4())

    await delete_session(redis_client, session_id)

    assert await get_session(redis_client, session_id) is None
