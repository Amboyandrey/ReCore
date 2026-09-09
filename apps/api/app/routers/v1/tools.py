"""Enable, list, and disable a workspace's tools — gated behind the `tools` flag, same shape
attachments' upload/list routes already use."""

import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.request_ip import client_ip
from app.deps.flags import flag_gate
from app.deps.workspace import WorkspaceCtx
from app.models import Role
from app.schemas.tool import ToolOut, WebSearchEnable
from app.services.audit import record_audit
from app.services.tools import disable_tool, enable_web_search, list_tools

router = APIRouter(prefix="/workspaces/{workspace_id}/tools", tags=["tools"])


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
    return ToolOut.model_validate(tool, from_attributes=True)


@router.get("", response_model=list[ToolOut])
async def list_tools_route(
    ctx: WorkspaceCtx = Depends(flag_gate("tools", minimum=Role.VIEWER)),
    db: AsyncSession = Depends(get_db),
) -> list[ToolOut]:
    """List every tool registered in the workspace."""
    tools = await list_tools(db, workspace_id=ctx.workspace_id)
    return [ToolOut.model_validate(t, from_attributes=True) for t in tools]


@router.delete("/{tool_id}", status_code=204)
async def disable_tool_route(
    tool_id: uuid.UUID,
    request: Request,
    ctx: WorkspaceCtx = Depends(flag_gate("tools", minimum=Role.VIEWER)),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Turn a tool off — re-enabling it later (POST /web-search again, for the built-in) keeps
    its configuration rather than starting over."""
    await disable_tool(db, workspace_id=ctx.workspace_id, tool_id=tool_id)
    await record_audit(
        db,
        actor_id=ctx.user.id,
        workspace_id=ctx.workspace_id,
        action="tool.disabled",
        target_type="tool",
        target_id=str(tool_id),
        ip=client_ip(request),
    )
