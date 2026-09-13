"""List the knowledge sources folded into a conversation's replies — gated behind the
`knowledge` flag, same shape tool_invocations.py's own conversation-scoped listing route uses."""

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.deps.flags import flag_gate
from app.deps.workspace import WorkspaceCtx
from app.models import Role
from app.schemas.knowledge import MessageSourceOut
from app.services.chat import get_conversation
from app.services.knowledge import list_message_sources

router = APIRouter(
    prefix="/workspaces/{workspace_id}/conversations/{conversation_id}/sources", tags=["knowledge"]
)


@router.get("", response_model=list[MessageSourceOut])
async def list_message_sources_route(
    conversation_id: uuid.UUID,
    ctx: WorkspaceCtx = Depends(flag_gate("knowledge", minimum=Role.VIEWER)),
    db: AsyncSession = Depends(get_db),
) -> list[MessageSourceOut]:
    """List every knowledge source used in a conversation — what lets a reloaded chat thread show
    a reply's sources, not just live while they stream in."""
    await get_conversation(
        db, workspace_id=ctx.workspace_id, conversation_id=conversation_id, viewer_id=ctx.user.id
    )
    sources = await list_message_sources(db, conversation_id=conversation_id)
    return [MessageSourceOut.model_validate(s, from_attributes=True) for s in sources]
