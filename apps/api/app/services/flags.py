"""The flag engine: resolution order, Redis caching, and pub/sub cache invalidation.

Resolution is strictly ordered, first match wins (see ARCHITECTURE.md #8):
1. A user override.
2. A workspace override.
3. A percentage rollout — deterministic per (flag, workspace), so a workspace never flickers
   between variants across requests.
4. The flag's default value.

Only the workspace-level result (override -> rollout -> default) is cached, at `flags:{ws}` —
user overrides are rare enough to read live on every call, so the cache never has to know who's
asking. An edit invalidates by deleting the affected snapshot(s) directly (we own the cache, no
need to wait out the TTL) and publishing on `flags:invalidate` for any other subscriber.
"""

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any, cast

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import FlagKeyAlreadyExists, FlagNotFound
from app.models import FeatureFlag, FlagOverride, FlagScope, FlagType

settings = get_settings()

INVALIDATION_CHANNEL = "flags:invalidate"


def _cache_key(workspace_id: uuid.UUID) -> str:
    """Where a workspace's resolved (override -> rollout -> default) flag snapshot lives."""
    return f"flags:{workspace_id}"


def _in_rollout(key: str, workspace_id: uuid.UUID, percentage: int) -> bool:
    """A stable 0-99 bucket per (flag, workspace) — the same workspace always lands the same side."""
    digest = hashlib.sha256(f"{key}:{workspace_id}".encode()).hexdigest()
    return int(digest, 16) % 100 < percentage


async def create_flag(
    db: AsyncSession,
    *,
    key: str,
    description: str,
    default_value: Any,
    rollout_percentage: int | None = None,
    flag_type: FlagType = FlagType.BOOLEAN,
) -> FeatureFlag:
    """Define a new flag. Keys are permanent — `key` carries a database-level unique constraint,
    so even an archived flag's key stays taken (a new flag with the same intent takes a new key)."""
    existing = await db.scalar(select(FeatureFlag).where(FeatureFlag.key == key))
    if existing is not None:
        raise FlagKeyAlreadyExists()
    flag = FeatureFlag(
        key=key,
        description=description,
        type=flag_type,
        default_value=default_value,
        rollout_percentage=rollout_percentage,
    )
    db.add(flag)
    await db.flush()
    return flag


async def list_flags(db: AsyncSession) -> list[FeatureFlag]:
    """List every non-archived flag definition, alphabetically by key."""
    stmt = select(FeatureFlag).where(FeatureFlag.archived_at.is_(None)).order_by(FeatureFlag.key)
    return list((await db.scalars(stmt)).all())


async def get_flag(db: AsyncSession, *, flag_id: uuid.UUID) -> FeatureFlag:
    """Load one flag definition by id, or raise if it doesn't exist."""
    flag = await db.get(FeatureFlag, flag_id)
    if flag is None:
        raise FlagNotFound()
    return flag


async def update_flag(
    db: AsyncSession, redis: Redis, *, flag_id: uuid.UUID, changes: dict[str, Any]
) -> FeatureFlag:
    """Apply only the fields present in `changes` (built with the request schema's
    `exclude_unset`) — so omitting a field leaves it untouched, but explicitly passing
    `rollout_percentage: null` clears it."""
    flag = await get_flag(db, flag_id=flag_id)
    if "description" in changes:
        flag.description = changes["description"]
    if "default_value" in changes:
        flag.default_value = changes["default_value"]
    if "rollout_percentage" in changes:
        flag.rollout_percentage = changes["rollout_percentage"]
    if "archived" in changes:
        flag.archived_at = datetime.now(UTC) if changes["archived"] else None
    await db.flush()
    # The definition changed, which can move the resolved value for every workspace at once.
    await invalidate_all_flag_caches(redis)
    return flag


async def _workspace_overrides_by_flag_id(
    db: AsyncSession, *, workspace_id: uuid.UUID
) -> dict[uuid.UUID, FlagOverride]:
    """This workspace's overrides, keyed by the flag they target, for one resolution pass."""
    stmt = select(FlagOverride).where(
        FlagOverride.scope == FlagScope.WORKSPACE, FlagOverride.scope_id == workspace_id
    )
    rows = (await db.scalars(stmt)).all()
    return {row.flag_id: row for row in rows}


def _resolve_workspace_value(
    flag: FeatureFlag, *, workspace_id: uuid.UUID, workspace_override: FlagOverride | None
) -> Any:
    """Steps 2-4 of the resolution order — the part that's the same for every user."""
    if workspace_override is not None:
        return workspace_override.value
    if flag.rollout_percentage is not None and _in_rollout(
        flag.key, workspace_id, flag.rollout_percentage
    ):
        return True
    return flag.default_value


async def _resolve_workspace_snapshot(
    db: AsyncSession, *, workspace_id: uuid.UUID
) -> dict[str, Any]:
    """Resolve every flag's value for a workspace, ignoring any user-level override."""
    flags = await list_flags(db)
    overrides = await _workspace_overrides_by_flag_id(db, workspace_id=workspace_id)
    return {
        flag.key: _resolve_workspace_value(
            flag, workspace_id=workspace_id, workspace_override=overrides.get(flag.id)
        )
        for flag in flags
    }


async def _cached_workspace_snapshot(
    db: AsyncSession, redis: Redis, *, workspace_id: uuid.UUID
) -> dict[str, Any]:
    """The workspace-level snapshot, from Redis if fresh, otherwise recomputed and re-cached."""
    cached = await redis.get(_cache_key(workspace_id))
    if cached is not None:
        return cast(dict[str, Any], json.loads(cached))
    snapshot = await _resolve_workspace_snapshot(db, workspace_id=workspace_id)
    await redis.set(_cache_key(workspace_id), json.dumps(snapshot), ex=settings.flag_cache_ttl_seconds)
    return snapshot


async def evaluate_flags(
    db: AsyncSession, redis: Redis, *, workspace_id: uuid.UUID, user_id: uuid.UUID
) -> dict[str, Any]:
    """Resolve the whole flag map for one user in one workspace — what `/flags/evaluate` returns."""
    snapshot = await _cached_workspace_snapshot(db, redis, workspace_id=workspace_id)
    stmt = (
        select(FeatureFlag.key, FlagOverride.value)
        .join(FlagOverride, FlagOverride.flag_id == FeatureFlag.id)
        .where(
            FlagOverride.scope == FlagScope.USER,
            FlagOverride.scope_id == user_id,
            FeatureFlag.archived_at.is_(None),
        )
    )
    user_overrides: dict[str, Any] = dict((await db.execute(stmt)).tuples().all())
    return {**snapshot, **user_overrides}


async def evaluate_flag(
    db: AsyncSession, redis: Redis, *, key: str, workspace_id: uuid.UUID, user_id: uuid.UUID
) -> Any:
    """Resolve one flag by key — used by `flag_gate()` and the provider killswitch check.

    A key with no definition resolves to `False` (fail closed) rather than an error, so gating
    a route on a flag nobody created yet behaves like the feature is off, not broken.
    """
    flags = await evaluate_flags(db, redis, workspace_id=workspace_id, user_id=user_id)
    return flags.get(key, False)


async def invalidate_workspace_cache(redis: Redis, *, workspace_id: uuid.UUID) -> None:
    """Drop one workspace's cached snapshot right away and publish for any other subscriber."""
    await redis.delete(_cache_key(workspace_id))
    await redis.publish(INVALIDATION_CHANNEL, str(workspace_id))


async def invalidate_all_flag_caches(redis: Redis) -> None:
    """Drop every workspace's cached snapshot — used when a flag definition itself changes.

    A SCAN over `flags:*` is fine at this scale: flag definitions change rarely, unlike the
    per-request reads the cache exists to absorb.
    """
    cursor = 0
    while True:
        cursor, keys = await redis.scan(cursor=cursor, match="flags:*", count=100)
        if keys:
            await redis.delete(*keys)
        if cursor == 0:
            break
    await redis.publish(INVALIDATION_CHANNEL, "*")


async def list_overrides(db: AsyncSession, *, flag_id: uuid.UUID) -> list[FlagOverride]:
    """List every override pinned to one flag, oldest first."""
    stmt = (
        select(FlagOverride).where(FlagOverride.flag_id == flag_id).order_by(FlagOverride.created_at)
    )
    return list((await db.scalars(stmt)).all())


async def set_override(
    db: AsyncSession,
    redis: Redis,
    *,
    flag_id: uuid.UUID,
    scope: FlagScope,
    scope_id: uuid.UUID,
    value: Any,
) -> FlagOverride:
    """Pin a flag to a value for one user or workspace, replacing any existing override there."""
    await get_flag(db, flag_id=flag_id)  # 404s if the flag doesn't exist
    existing = await db.scalar(
        select(FlagOverride).where(
            FlagOverride.flag_id == flag_id,
            FlagOverride.scope == scope,
            FlagOverride.scope_id == scope_id,
        )
    )
    if existing is not None:
        existing.value = value
        override = existing
    else:
        override = FlagOverride(flag_id=flag_id, scope=scope, scope_id=scope_id, value=value)
        db.add(override)
    await db.flush()
    if scope == FlagScope.WORKSPACE:
        await invalidate_workspace_cache(redis, workspace_id=scope_id)
    return override


async def delete_override(
    db: AsyncSession, redis: Redis, *, flag_id: uuid.UUID, override_id: uuid.UUID
) -> None:
    """Remove one override, falling its target back to the rollout/default resolution."""
    override = await db.get(FlagOverride, override_id)
    if override is None or override.flag_id != flag_id:
        raise FlagNotFound("Override not found.")
    scope, scope_id = override.scope, override.scope_id
    await db.delete(override)
    await db.flush()
    if scope == FlagScope.WORKSPACE:
        await invalidate_workspace_cache(redis, workspace_id=scope_id)
