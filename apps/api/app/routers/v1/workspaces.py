"""Create, list, and fetch workspaces — membership and invitations live in their own routers."""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.request_ip import client_ip
from app.deps.auth import get_current_user
from app.deps.workspace import WorkspaceCtx, get_workspace_ctx
from app.models import Role, User
from app.schemas.workspace import WorkspaceCreate, WorkspaceOut
from app.services.audit import record_audit
from app.services.workspaces import create_workspace, list_my_workspaces

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


@router.post("", status_code=201, response_model=WorkspaceOut)
async def create_workspace_route(
    body: WorkspaceCreate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WorkspaceOut:
    """Create a workspace and make the caller its owner."""
    workspace = await create_workspace(db, owner=user, name=body.name)
    await record_audit(
        db,
        actor_id=user.id,
        workspace_id=workspace.id,
        action="workspace.created",
        target_type="workspace",
        target_id=str(workspace.id),
        ip=client_ip(request),
        metadata={"name": workspace.name},
    )
    return WorkspaceOut(
        id=workspace.id, slug=workspace.slug, name=workspace.name, role=Role.OWNER,
        created_at=workspace.created_at,
    )


@router.get("", response_model=list[WorkspaceOut])
async def list_workspaces_route(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[WorkspaceOut]:
    """List every workspace the caller belongs to, with their role in each."""
    rows = await list_my_workspaces(db, user=user)
    return [
        WorkspaceOut(id=w.id, slug=w.slug, name=w.name, role=role, created_at=w.created_at)
        for w, role in rows
    ]


@router.get("/{workspace_id}", response_model=WorkspaceOut)
async def get_workspace_route(ctx: WorkspaceCtx = Depends(get_workspace_ctx)) -> WorkspaceOut:
    """Return the workspace named in the path, if the caller is a member of it."""
    return WorkspaceOut(
        id=ctx.workspace.id, slug=ctx.workspace.slug, name=ctx.workspace.name, role=ctx.role,
        created_at=ctx.workspace.created_at,
    )
