"""List, promote/demote, and remove workspace members."""

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.deps.workspace import WorkspaceCtx, require_role
from app.models import Role, User
from app.schemas.member import MemberOut, RoleUpdate
from app.services.members import change_member_role, list_members, remove_member

router = APIRouter(prefix="/workspaces/{workspace_id}/members", tags=["members"])


@router.get("", response_model=list[MemberOut])
async def list_members_route(
    ctx: WorkspaceCtx = Depends(require_role(Role.VIEWER)),
    db: AsyncSession = Depends(get_db),
) -> list[MemberOut]:
    """List every member of the workspace, in the order they joined."""
    rows = await list_members(db, workspace_id=ctx.workspace_id)
    return [MemberOut(user_id=u.id, email=u.email, role=m.role, joined_at=m.joined_at) for m, u in rows]


@router.patch("/{user_id}", response_model=MemberOut)
async def change_role_route(
    user_id: uuid.UUID,
    body: RoleUpdate,
    ctx: WorkspaceCtx = Depends(require_role(Role.ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> MemberOut:
    """Change a member's role."""
    member = await change_member_role(
        db,
        workspace_id=ctx.workspace_id,
        acting_role=ctx.role,
        target_user_id=user_id,
        new_role=body.role,
    )
    user = await db.get(User, user_id)
    assert user is not None  # change_member_role already confirmed this membership exists
    return MemberOut(user_id=user.id, email=user.email, role=member.role, joined_at=member.joined_at)


@router.delete("/{user_id}", status_code=204)
async def remove_member_route(
    user_id: uuid.UUID,
    ctx: WorkspaceCtx = Depends(require_role(Role.ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Remove a member from the workspace."""
    await remove_member(db, workspace_id=ctx.workspace_id, acting_role=ctx.role, target_user_id=user_id)
