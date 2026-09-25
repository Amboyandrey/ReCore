"""Connect, list, re-sync, update, and remove a workspace's MCP servers — gated behind the `tools`
flag, since what a server contributes is tools like any other."""

import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.request_ip import client_ip
from app.deps.flags import flag_gate
from app.deps.workspace import WorkspaceCtx
from app.models import McpServer, Role
from app.schemas.mcp_server import McpServerCreate, McpServerOut, McpServerUpdate
from app.services.audit import record_audit
from app.services.mcp_servers import (
    create_mcp_server,
    delete_mcp_server,
    get_mcp_server,
    list_mcp_servers,
    sync_mcp_server,
    update_mcp_server,
)

router = APIRouter(prefix="/workspaces/{workspace_id}/mcp-servers", tags=["mcp-servers"])


def _to_out(server: McpServer) -> McpServerOut:
    """A server's row, minus its secret's actual value."""
    return McpServerOut(
        id=server.id,
        name=server.name,
        url=server.url,
        auth_header=server.auth_header,
        has_secret=server.ciphertext is not None,
        last_synced_at=server.last_synced_at,
        created_at=server.created_at,
    )


@router.post("", status_code=201, response_model=McpServerOut)
async def create_mcp_server_route(
    body: McpServerCreate,
    request: Request,
    ctx: WorkspaceCtx = Depends(flag_gate("tools", minimum=Role.VIEWER)),
    db: AsyncSession = Depends(get_db),
) -> McpServerOut:
    """Connect a server and import its tools, disabled until someone turns them on."""
    server = await create_mcp_server(
        db,
        workspace_id=ctx.workspace_id,
        created_by=ctx.user,
        name=body.name,
        url=body.url,
        auth_header=body.auth_header,
        auth_value=body.auth_value,
    )
    await record_audit(
        db,
        actor_id=ctx.user.id,
        workspace_id=ctx.workspace_id,
        action="mcp_server.created",
        target_type="mcp_server",
        target_id=str(server.id),
        ip=client_ip(request),
        metadata={"name": server.name, "url": server.url},
    )
    return _to_out(server)


@router.get("", response_model=list[McpServerOut])
async def list_mcp_servers_route(
    ctx: WorkspaceCtx = Depends(flag_gate("tools", minimum=Role.VIEWER)),
    db: AsyncSession = Depends(get_db),
) -> list[McpServerOut]:
    """List every server connected to the workspace."""
    return [_to_out(s) for s in await list_mcp_servers(db, workspace_id=ctx.workspace_id)]


@router.post("/{server_id}/sync", response_model=McpServerOut)
async def sync_mcp_server_route(
    server_id: uuid.UUID,
    request: Request,
    ctx: WorkspaceCtx = Depends(flag_gate("tools", minimum=Role.VIEWER)),
    db: AsyncSession = Depends(get_db),
) -> McpServerOut:
    """Re-read the server's tool list, picking up added, changed, and removed tools."""
    server = await get_mcp_server(db, workspace_id=ctx.workspace_id, server_id=server_id)
    await sync_mcp_server(db, server=server)
    await record_audit(
        db,
        actor_id=ctx.user.id,
        workspace_id=ctx.workspace_id,
        action="mcp_server.synced",
        target_type="mcp_server",
        target_id=str(server.id),
        ip=client_ip(request),
    )
    return _to_out(server)


@router.patch("/{server_id}", response_model=McpServerOut)
async def update_mcp_server_route(
    server_id: uuid.UUID,
    body: McpServerUpdate,
    request: Request,
    ctx: WorkspaceCtx = Depends(flag_gate("tools", minimum=Role.VIEWER)),
    db: AsyncSession = Depends(get_db),
) -> McpServerOut:
    """Change a server's URL or auth, then re-sync its tools against the new details."""
    changes = body.model_dump(exclude_unset=True)
    server = await update_mcp_server(db, workspace_id=ctx.workspace_id, server_id=server_id, changes=changes)
    await record_audit(
        db,
        actor_id=ctx.user.id,
        workspace_id=ctx.workspace_id,
        action="mcp_server.updated",
        target_type="mcp_server",
        target_id=str(server.id),
        ip=client_ip(request),
        metadata={"changes": [k for k in changes if k != "auth_value"]},
    )
    return _to_out(server)


@router.delete("/{server_id}", status_code=204)
async def delete_mcp_server_route(
    server_id: uuid.UUID,
    request: Request,
    ctx: WorkspaceCtx = Depends(flag_gate("tools", minimum=Role.VIEWER)),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Remove a server along with every tool it contributed."""
    await delete_mcp_server(db, workspace_id=ctx.workspace_id, server_id=server_id)
    await record_audit(
        db,
        actor_id=ctx.user.id,
        workspace_id=ctx.workspace_id,
        action="mcp_server.deleted",
        target_type="mcp_server",
        target_id=str(server_id),
        ip=client_ip(request),
    )
