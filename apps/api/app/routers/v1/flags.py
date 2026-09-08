"""Resolve a workspace's flags for chat, and the superuser-only admin CRUD behind `/admin/flags`."""

import uuid
from typing import Any

from fastapi import APIRouter, Depends
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.redis import get_redis
from app.deps.flags import require_superuser
from app.deps.workspace import WorkspaceCtx, require_role
from app.models import Role, User
from app.schemas.flag import FlagCreate, FlagOut, FlagUpdate, OverrideCreate, OverrideOut
from app.services.flags import (
    create_flag,
    delete_override,
    evaluate_flags,
    list_flags,
    list_overrides,
    set_override,
    update_flag,
)

evaluate_router = APIRouter(prefix="/workspaces/{workspace_id}/flags", tags=["flags"])
admin_router = APIRouter(prefix="/admin/flags", tags=["flags-admin"])


@evaluate_router.get("/evaluate")
async def evaluate_flags_route(
    ctx: WorkspaceCtx = Depends(require_role(Role.VIEWER)),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Resolve every flag for the caller in this workspace — one call, the whole map."""
    return await evaluate_flags(db, redis, workspace_id=ctx.workspace_id, user_id=ctx.user.id)


@admin_router.post("", status_code=201, response_model=FlagOut)
async def create_flag_route(
    body: FlagCreate,
    _admin: User = Depends(require_superuser),
    db: AsyncSession = Depends(get_db),
) -> FlagOut:
    """Define a new flag, superuser only."""
    flag = await create_flag(
        db,
        key=body.key,
        description=body.description,
        default_value=body.default_value,
        rollout_percentage=body.rollout_percentage,
    )
    return FlagOut.model_validate(flag, from_attributes=True)


@admin_router.get("", response_model=list[FlagOut])
async def list_flags_route(
    _admin: User = Depends(require_superuser),
    db: AsyncSession = Depends(get_db),
) -> list[FlagOut]:
    """List every non-archived flag definition."""
    flags = await list_flags(db)
    return [FlagOut.model_validate(f, from_attributes=True) for f in flags]


@admin_router.patch("/{flag_id}", response_model=FlagOut)
async def update_flag_route(
    flag_id: uuid.UUID,
    body: FlagUpdate,
    _admin: User = Depends(require_superuser),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> FlagOut:
    """Edit a flag's description, default, rollout, or archived state — only what's sent changes."""
    flag = await update_flag(db, redis, flag_id=flag_id, changes=body.model_dump(exclude_unset=True))
    return FlagOut.model_validate(flag, from_attributes=True)


@admin_router.get("/{flag_id}/overrides", response_model=list[OverrideOut])
async def list_overrides_route(
    flag_id: uuid.UUID,
    _admin: User = Depends(require_superuser),
    db: AsyncSession = Depends(get_db),
) -> list[OverrideOut]:
    """List every override pinned to one flag."""
    overrides = await list_overrides(db, flag_id=flag_id)
    return [OverrideOut.model_validate(o, from_attributes=True) for o in overrides]


@admin_router.post("/{flag_id}/overrides", status_code=201, response_model=OverrideOut)
async def set_override_route(
    flag_id: uuid.UUID,
    body: OverrideCreate,
    _admin: User = Depends(require_superuser),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> OverrideOut:
    """Pin a flag to a value for one user or workspace — the killswitch and per-tenant demo lever."""
    override = await set_override(
        db, redis, flag_id=flag_id, scope=body.scope, scope_id=body.scope_id, value=body.value
    )
    return OverrideOut.model_validate(override, from_attributes=True)


@admin_router.delete("/{flag_id}/overrides/{override_id}", status_code=204)
async def delete_override_route(
    flag_id: uuid.UUID,
    override_id: uuid.UUID,
    _admin: User = Depends(require_superuser),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> None:
    """Remove an override, falling its target back to the rollout/default resolution."""
    await delete_override(db, redis, flag_id=flag_id, override_id=override_id)
