"""Creating and listing invites (workspace-scoped), and redeeming one by its token (not scoped —
the token itself, not workspace membership, is what authorizes reading and accepting an invite)."""

from fastapi import APIRouter, Depends, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_db
from app.core.errors import RateLimited
from app.core.redis import get_redis
from app.core.request_ip import client_ip
from app.deps.auth import get_current_user
from app.deps.workspace import WorkspaceCtx, require_role
from app.models import Role, User, Workspace
from app.schemas.member import InviteCreate, InviteOut, InvitePreview
from app.schemas.workspace import WorkspaceOut
from app.services.audit import record_audit
from app.services.invitations import (
    accept_invitation,
    create_invitation,
    get_invitation_by_token,
    list_pending_invitations,
)
from app.services.rate_limit import check_rate_limit

settings = get_settings()

workspace_router = APIRouter(prefix="/workspaces/{workspace_id}/invitations", tags=["invitations"])
token_router = APIRouter(prefix="/invitations", tags=["invitations"])


@workspace_router.post("", status_code=201, response_model=InviteOut)
async def create_invitation_route(
    body: InviteCreate,
    request: Request,
    ctx: WorkspaceCtx = Depends(require_role(Role.ADMIN)),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> InviteOut:
    """Invite someone to the workspace. The token is only ever shown in this response."""
    allowed = await check_rate_limit(
        redis,
        f"rl:invite:{ctx.workspace_id}",
        limit=settings.auth_rate_limit_max,
        window_seconds=settings.auth_rate_limit_window_seconds,
    )
    if not allowed:
        raise RateLimited()
    invitation, token = await create_invitation(
        db,
        workspace_id=ctx.workspace_id,
        invited_by=ctx.user,
        acting_role=ctx.role,
        email=body.email,
        role=body.role,
    )
    await record_audit(
        db,
        actor_id=ctx.user.id,
        workspace_id=ctx.workspace_id,
        action="invitation.created",
        target_type="invitation",
        target_id=str(invitation.id),
        ip=client_ip(request),
        metadata={"email": invitation.email, "role": invitation.role.value},
    )
    return InviteOut(
        id=invitation.id,
        email=invitation.email,
        role=invitation.role,
        expires_at=invitation.expires_at,
        token=token,
    )


@workspace_router.get("", response_model=list[InviteOut])
async def list_invitations_route(
    ctx: WorkspaceCtx = Depends(require_role(Role.ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> list[InviteOut]:
    """List invitations that haven't been accepted yet."""
    invitations = await list_pending_invitations(db, workspace_id=ctx.workspace_id)
    return [
        InviteOut(id=i.id, email=i.email, role=i.role, expires_at=i.expires_at) for i in invitations
    ]


@token_router.get("/{token}", response_model=InvitePreview)
async def preview_invitation_route(token: str, db: AsyncSession = Depends(get_db)) -> InvitePreview:
    """Preview an invitation before signing in — reachable without an account."""
    invitation = await get_invitation_by_token(db, token)
    workspace = await db.get(Workspace, invitation.workspace_id)
    assert workspace is not None  # a workspace is never deleted while it still has invitations
    return InvitePreview(
        workspace_name=workspace.name,
        email=invitation.email,
        role=invitation.role,
        expires_at=invitation.expires_at,
    )


@token_router.post("/{token}/accept", response_model=WorkspaceOut)
async def accept_invitation_route(
    token: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WorkspaceOut:
    """Accept an invitation — the signed-in account's email must match the one invited."""
    invitation = await get_invitation_by_token(db, token)
    member = await accept_invitation(db, invitation=invitation, user=user)
    workspace = await db.get(Workspace, invitation.workspace_id)
    assert workspace is not None
    await record_audit(
        db,
        actor_id=user.id,
        workspace_id=workspace.id,
        action="invitation.accepted",
        target_type="invitation",
        target_id=str(invitation.id),
        ip=client_ip(request),
        metadata={"role": member.role.value},
    )
    return WorkspaceOut(
        id=workspace.id, slug=workspace.slug, name=workspace.name, role=member.role,
        created_at=workspace.created_at,
    )
