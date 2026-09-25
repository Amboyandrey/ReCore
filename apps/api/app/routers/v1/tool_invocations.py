"""List the tool calls made in a conversation — gated behind the `tools` flag, same shape
attachments' own conversation-scoped listing route already uses."""

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.deps.flags import flag_gate
from app.deps.workspace import WorkspaceCtx
from app.models import Role
from app.schemas.tool import ToolImageOut, ToolInvocationOut
from app.services.attachments import list_tool_images
from app.services.chat import get_conversation
from app.services.tools import list_tool_invocations

router = APIRouter(
    prefix="/workspaces/{workspace_id}/conversations/{conversation_id}/tool-invocations",
    tags=["tools"],
)


@router.get("", response_model=list[ToolInvocationOut])
async def list_tool_invocations_route(
    conversation_id: uuid.UUID,
    ctx: WorkspaceCtx = Depends(flag_gate("tools", minimum=Role.VIEWER)),
    db: AsyncSession = Depends(get_db),
) -> list[ToolInvocationOut]:
    """List every tool call made in a conversation — what lets a reloaded chat thread show a
    tool call was made on an earlier turn, not just live while it's streaming in."""
    await get_conversation(
        db, workspace_id=ctx.workspace_id, conversation_id=conversation_id, viewer_id=ctx.user.id
    )
    invocations = await list_tool_invocations(db, conversation_id=conversation_id)
    images = await list_tool_images(db, tool_invocation_ids=[i.id for i in invocations])
    return [
        ToolInvocationOut.model_validate(i, from_attributes=True).model_copy(
            update={"images": [ToolImageOut(id=a.id, mime=a.mime) for a in images.get(i.id, [])]}
        )
        for i in invocations
    ]
