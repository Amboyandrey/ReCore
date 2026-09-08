"""The flag engine's resolution order, its Redis cache, and pub/sub invalidation."""

import uuid

import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import FlagKeyAlreadyExists, FlagNotFound
from app.models import FlagOverride, FlagScope
from app.services.flags import (
    INVALIDATION_CHANNEL,
    create_flag,
    delete_override,
    evaluate_flag,
    evaluate_flags,
    invalidate_workspace_cache,
    list_flags,
    set_override,
    update_flag,
)


def _key() -> str:
    """A fresh, collision-free flag key for each test — feature_flags isn't truncated between
    tests (see conftest.py), so reusing a literal key across tests would trip the unique index."""
    return f"test.{uuid.uuid4().hex}"


async def test_default_value_applies_with_no_overrides_or_rollout(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    """With nothing else set, a flag simply resolves to its default."""
    flag = await create_flag(db_session, key=_key(), description="d", default_value=True)

    value = await evaluate_flag(
        db_session, redis_client, key=flag.key, workspace_id=uuid.uuid4(), user_id=uuid.uuid4()
    )

    assert value is True


async def test_workspace_override_beats_the_default(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    """A workspace override wins over the flag's default."""
    flag = await create_flag(db_session, key=_key(), description="d", default_value=True)
    workspace_id = uuid.uuid4()
    await set_override(
        db_session,
        redis_client,
        flag_id=flag.id,
        scope=FlagScope.WORKSPACE,
        scope_id=workspace_id,
        value=False,
    )

    value = await evaluate_flag(
        db_session, redis_client, key=flag.key, workspace_id=workspace_id, user_id=uuid.uuid4()
    )

    assert value is False


async def test_user_override_beats_the_workspace_override(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    """The resolution order's top tier: a user override beats even a workspace override."""
    flag = await create_flag(db_session, key=_key(), description="d", default_value=False)
    workspace_id, user_id = uuid.uuid4(), uuid.uuid4()
    await set_override(
        db_session,
        redis_client,
        flag_id=flag.id,
        scope=FlagScope.WORKSPACE,
        scope_id=workspace_id,
        value=False,
    )
    await set_override(
        db_session, redis_client, flag_id=flag.id, scope=FlagScope.USER, scope_id=user_id, value=True
    )

    value = await evaluate_flag(
        db_session, redis_client, key=flag.key, workspace_id=workspace_id, user_id=user_id
    )

    assert value is True


async def test_full_rollout_enables_regardless_of_workspace(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    """rollout_percentage=100 always lands in the rollout bucket, whatever the workspace id."""
    flag = await create_flag(
        db_session, key=_key(), description="d", default_value=False, rollout_percentage=100
    )

    value = await evaluate_flag(
        db_session, redis_client, key=flag.key, workspace_id=uuid.uuid4(), user_id=uuid.uuid4()
    )

    assert value is True


async def test_zero_rollout_never_enables_and_falls_back_to_default(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    """rollout_percentage=0 never lands in the bucket, so the default always applies instead."""
    flag = await create_flag(
        db_session, key=_key(), description="d", default_value=False, rollout_percentage=0
    )

    value = await evaluate_flag(
        db_session, redis_client, key=flag.key, workspace_id=uuid.uuid4(), user_id=uuid.uuid4()
    )

    assert value is False


async def test_a_missing_flag_key_resolves_to_false(db_session: AsyncSession, redis_client: Redis) -> None:
    """No definition at all is treated as disabled, not an error — gates fail closed."""
    value = await evaluate_flag(
        db_session, redis_client, key=_key(), workspace_id=uuid.uuid4(), user_id=uuid.uuid4()
    )

    assert value is False


async def test_creating_a_flag_with_a_taken_key_raises(db_session: AsyncSession) -> None:
    """Two live flags can never share a key."""
    key = _key()
    await create_flag(db_session, key=key, description="d", default_value=True)

    with pytest.raises(FlagKeyAlreadyExists):
        await create_flag(db_session, key=key, description="d2", default_value=False)


async def test_update_flag_only_touches_fields_that_were_sent(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    """Fields absent from `changes` are left exactly as they were."""
    flag = await create_flag(
        db_session, key=_key(), description="original", default_value=False, rollout_percentage=10
    )

    updated = await update_flag(
        db_session, redis_client, flag_id=flag.id, changes={"default_value": True}
    )

    assert updated.default_value is True
    assert updated.description == "original"
    assert updated.rollout_percentage == 10


async def test_archiving_a_flag_removes_it_from_the_listing(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    """An archived flag drops out of list_flags — but its key stays taken (see create_flag)."""
    flag = await create_flag(db_session, key=_key(), description="d", default_value=True)

    await update_flag(db_session, redis_client, flag_id=flag.id, changes={"archived": True})

    keys = {f.key for f in await list_flags(db_session)}
    assert flag.key not in keys


async def test_deleting_an_override_falls_back_to_the_default(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    """Removing an override un-pins its target — resolution moves back down the order."""
    flag = await create_flag(db_session, key=_key(), description="d", default_value=True)
    workspace_id = uuid.uuid4()
    override = await set_override(
        db_session,
        redis_client,
        flag_id=flag.id,
        scope=FlagScope.WORKSPACE,
        scope_id=workspace_id,
        value=False,
    )
    disabled = await evaluate_flag(
        db_session, redis_client, key=flag.key, workspace_id=workspace_id, user_id=uuid.uuid4()
    )
    assert disabled is False

    await delete_override(db_session, redis_client, flag_id=flag.id, override_id=override.id)

    value = await evaluate_flag(
        db_session, redis_client, key=flag.key, workspace_id=workspace_id, user_id=uuid.uuid4()
    )
    assert value is True


async def test_deleting_an_override_under_the_wrong_flag_is_not_found(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    """An override id that exists, but under a different flag, is treated as not found."""
    flag_a = await create_flag(db_session, key=_key(), description="d", default_value=True)
    flag_b = await create_flag(db_session, key=_key(), description="d", default_value=True)
    override = await set_override(
        db_session,
        redis_client,
        flag_id=flag_a.id,
        scope=FlagScope.WORKSPACE,
        scope_id=uuid.uuid4(),
        value=False,
    )

    with pytest.raises(FlagNotFound):
        await delete_override(db_session, redis_client, flag_id=flag_b.id, override_id=override.id)


async def test_a_cached_workspace_snapshot_reflects_a_stale_value_until_invalidated(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    """The 30s cache means a direct override write is only picked up once its key is invalidated
    — set_override does this itself, but this test drives it manually to prove the cache is real."""
    flag = await create_flag(db_session, key=_key(), description="d", default_value=False)
    workspace_id = uuid.uuid4()
    # Prime the cache with the default (no override yet).
    assert await evaluate_flags(db_session, redis_client, workspace_id=workspace_id, user_id=uuid.uuid4())

    # Write an override directly at the DB layer, bypassing set_override's own invalidation.
    db_session.add(
        FlagOverride(flag_id=flag.id, scope=FlagScope.WORKSPACE, scope_id=workspace_id, value=True)
    )
    await db_session.flush()
    stale = await evaluate_flag(
        db_session, redis_client, key=flag.key, workspace_id=workspace_id, user_id=uuid.uuid4()
    )
    assert stale is False  # the cached snapshot hasn't seen the new row yet

    await invalidate_workspace_cache(redis_client, workspace_id=workspace_id)
    fresh = await evaluate_flag(
        db_session, redis_client, key=flag.key, workspace_id=workspace_id, user_id=uuid.uuid4()
    )
    assert fresh is True


async def test_invalidation_is_published_for_any_other_subscriber(redis_client: Redis) -> None:
    """Same pattern as the generation stop signal: a real pub/sub message, not just a cache DEL."""
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(INVALIDATION_CHANNEL)
    await pubsub.get_message(timeout=1)  # the subscribe confirmation itself

    workspace_id = uuid.uuid4()
    await invalidate_workspace_cache(redis_client, workspace_id=workspace_id)
    message = await pubsub.get_message(timeout=1)

    assert message is not None
    assert message["data"] == str(workspace_id)
    await pubsub.aclose()
