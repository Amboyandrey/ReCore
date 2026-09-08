"""The dependency chain every workspace-scoped route runs through: user -> membership -> role."""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db, set_workspace_scope
from app.core.errors import InsufficientRole, WorkspaceNotFound
from app.deps.auth import get_current_user
from app.models import Role, User, Workspace, WorkspaceMember
from app.models.role import role_at_least


@dataclass(frozen=True)
class WorkspaceCtx:
    """Everything a workspace-scoped endpoint needs: which workspace, which user, at what role."""

    workspace: Workspace
    user: User
    role: Role

    @property
    def workspace_id(self) -> uuid.UUID:
        """The id services filter every workspace-scoped query on."""
        return self.workspace.id


async def get_workspace_ctx(
    workspace_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WorkspaceCtx:
    """Load the caller's membership in the path's workspace, or 404 if they aren't a member.

    `workspace_id` is bound from the path by name — every router mounting this dependency must
    declare `{workspace_id}` in its own route path.
    """
    stmt = (
        select(Workspace, WorkspaceMember.role)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
        .where(
            Workspace.id == workspace_id,
            Workspace.deleted_at.is_(None),
            WorkspaceMember.user_id == user.id,
        )
    )
    row = (await db.execute(stmt)).first()
    if row is None:
        raise WorkspaceNotFound()
    workspace, role = row
    # Everything this request queries from here on is scoped to this workspace — row-level
    # security policies on every tenant table check exactly this, as a backstop under this
    # membership check, not instead of it (see docs/ARCHITECTURE.md #4's tenancy design).
    await set_workspace_scope(db, workspace_id)
    return WorkspaceCtx(workspace=workspace, user=user, role=role)


def require_role(minimum: Role) -> Callable[..., Awaitable[WorkspaceCtx]]:
    """Build a dependency that raises 403 unless the caller's role meets `minimum` or higher."""

    async def check(ctx: WorkspaceCtx = Depends(get_workspace_ctx)) -> WorkspaceCtx:
        """Enforce the role floor this route was declared with."""
        if not role_at_least(ctx.role, minimum):
            raise InsufficientRole()
        return ctx

    return check
