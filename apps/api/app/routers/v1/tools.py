"""Register, list, update, and remove a workspace's tools — gated behind the `tools` flag, same
shape attachments' upload/list routes already use."""

import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.request_ip import client_ip
from app.deps.flags import flag_gate
from app.deps.workspace import WorkspaceCtx
from app.models import Role, Tool
from app.schemas.tool import HttpToolCreate, ToolOut, ToolUpdate, WebSearchEnable
from app.services.audit import record_audit
from app.services.tools import create_http_tool, delete_tool, enable_web_search, list_tools, update_tool

router = APIRouter(prefix="/workspaces/{workspace_id}/tools", tags=["tools"])


def _to_tool_out(tool: Tool) -> ToolOut:
    """A tool's row, minus its secret's actual value — `has_secret` is all a caller ever needs
    to know about it (whether to prompt "set a key" or "rotate it")."""
    return ToolOut(
        id=tool.id,
        name=tool.name,
        description=tool.description,
        parameters=tool.parameters,
        kind=tool.kind,
        enabled=tool.enabled,
        method=tool.method,
        url=tool.url,
        secret_header=tool.secret_header,
        has_secret=tool.ciphertext is not None,
        mcp_server_id=tool.mcp_server_id,
        created_at=tool.created_at,
    )


@router.post("/web-search", status_code=201, response_model=ToolOut)
async def enable_web_search_route(
    body: WebSearchEnable,
    request: Request,
    ctx: WorkspaceCtx = Depends(flag_gate("tools", minimum=Role.VIEWER)),
    db: AsyncSession = Depends(get_db),
) -> ToolOut:
    """Turn on the built-in web search tool, or rotate its stored Tavily key."""
    tool = await enable_web_search(
        db, workspace_id=ctx.workspace_id, created_by=ctx.user, api_key=body.api_key
    )
    await record_audit(
        db,
        actor_id=ctx.user.id,
        workspace_id=ctx.workspace_id,
        action="tool.enabled",
        target_type="tool",
        target_id=str(tool.id),
        ip=client_ip(request),
        metadata={"name": tool.name},
    )
    return _to_tool_out(tool)


@router.post("", status_code=201, response_model=ToolOut)
async def create_http_tool_route(
    body: HttpToolCreate,
    request: Request,
    ctx: WorkspaceCtx = Depends(flag_gate("tools", minimum=Role.VIEWER)),
    db: AsyncSession = Depends(get_db),
) -> ToolOut:
    """Register a third-party HTTP tool — its URL is checked against the SSRF guard immediately,
    and again before every call."""
    tool = await create_http_tool(
        db,
        workspace_id=ctx.workspace_id,
        created_by=ctx.user,
        name=body.name,
        description=body.description,
        parameters=body.parameters,
        method=body.method,
        url=body.url,
        secret_header=body.secret_header,
        secret_value=body.secret_value,
    )
    await record_audit(
        db,
        actor_id=ctx.user.id,
        workspace_id=ctx.workspace_id,
        action="tool.created",
        target_type="tool",
        target_id=str(tool.id),
        ip=client_ip(request),
        metadata={"name": tool.name, "method": tool.method, "url": tool.url},
    )
    return _to_tool_out(tool)


@router.get("", response_model=list[ToolOut])
async def list_tools_route(
    ctx: WorkspaceCtx = Depends(flag_gate("tools", minimum=Role.VIEWER)),
    db: AsyncSession = Depends(get_db),
) -> list[ToolOut]:
    """List every tool registered in the workspace."""
    tools = await list_tools(db, workspace_id=ctx.workspace_id)
    return [_to_tool_out(t) for t in tools]


@router.patch("/{tool_id}", response_model=ToolOut)
async def update_tool_route(
    tool_id: uuid.UUID,
    body: ToolUpdate,
    request: Request,
    ctx: WorkspaceCtx = Depends(flag_gate("tools", minimum=Role.VIEWER)),
    db: AsyncSession = Depends(get_db),
) -> ToolOut:
    """Toggle a tool on/off, or edit an HTTP tool's configuration — only the fields sent change."""
    changes = body.model_dump(exclude_unset=True)
    tool = await update_tool(db, workspace_id=ctx.workspace_id, tool_id=tool_id, changes=changes)
    await record_audit(
        db,
        actor_id=ctx.user.id,
        workspace_id=ctx.workspace_id,
        action="tool.updated",
        target_type="tool",
        target_id=str(tool.id),
        ip=client_ip(request),
        metadata={"changes": [k for k in changes if k != "secret_value"]},
    )
    return _to_tool_out(tool)


@router.delete("/{tool_id}", status_code=204)
async def delete_tool_route(
    tool_id: uuid.UUID,
    request: Request,
    ctx: WorkspaceCtx = Depends(flag_gate("tools", minimum=Role.VIEWER)),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Permanently remove a tool — to just turn one off without losing its configuration, use
    PATCH with `{"enabled": false}` instead."""
    await delete_tool(db, workspace_id=ctx.workspace_id, tool_id=tool_id)
    await record_audit(
        db,
        actor_id=ctx.user.id,
        workspace_id=ctx.workspace_id,
        action="tool.deleted",
        target_type="tool",
        target_id=str(tool_id),
        ip=client_ip(request),
    )
