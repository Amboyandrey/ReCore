"""Dependencies that gate a route behind a feature flag, or an action behind superuser status."""

from collections.abc import Awaitable, Callable

from fastapi import Depends
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.errors import FeatureDisabled, SuperuserRequired
from app.core.redis import get_redis
from app.deps.auth import get_current_user
from app.deps.workspace import WorkspaceCtx, require_role
from app.models import Role, User
from app.services.flags import evaluate_flag


async def require_superuser(user: User = Depends(get_current_user)) -> User:
    """Reject the request unless the caller's account is flagged as a superuser.

    403, not 404: `/admin/*` isn't tenant data whose existence needs hiding, it's a fixed set of
    platform routes — the caller just isn't allowed to use them.
    """
    if not user.is_superuser:
        raise SuperuserRequired()
    return user


def flag_gate(key: str, minimum: Role = Role.VIEWER) -> Callable[..., Awaitable[WorkspaceCtx]]:
    """Build a dependency that 404s a workspace-scoped route unless `key` resolves truthy for
    the caller — layered on top of the usual role check, not instead of it."""

    async def check(
        ctx: WorkspaceCtx = Depends(require_role(minimum)),
        db: AsyncSession = Depends(get_db),
        redis: Redis = Depends(get_redis),
    ) -> WorkspaceCtx:
        """Evaluate the flag for this caller's (workspace, user) pair and enforce it."""
        enabled = await evaluate_flag(
            db, redis, key=key, workspace_id=ctx.workspace_id, user_id=ctx.user.id
        )
        if not enabled:
            raise FeatureDisabled()
        return ctx

    return check
