"""Create, list, update, and remove a workspace's saved assistants — open to any member, same
floor sending a message or registering a tool already has, since an assistant is only ever
usable inside a chat."""

import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.request_ip import client_ip
from app.deps.workspace import WorkspaceCtx, require_role
from app.models import Assistant, Role
from app.schemas.assistant import AssistantCreate, AssistantOut, AssistantUpdate
from app.services.assistants import (
    create_assistant,
    delete_assistant,
    list_assistant_tool_ids,
    list_assistants,
    update_assistant,
)
from app.services.audit import record_audit

router = APIRouter(prefix="/workspaces/{workspace_id}/assistants", tags=["assistants"])


async def _to_assistant_out(db: AsyncSession, assistant: Assistant) -> AssistantOut:
    tool_ids = await list_assistant_tool_ids(db, assistant_id=assistant.id)
    return AssistantOut(
        id=assistant.id,
        name=assistant.name,
        instructions=assistant.instructions,
        model_id=assistant.model_id,
        tool_ids=tool_ids,
        memory_enabled=assistant.memory_enabled,
        created_by=assistant.created_by,
        created_at=assistant.created_at,
    )


@router.post("", status_code=201, response_model=AssistantOut)
async def create_assistant_route(
    body: AssistantCreate,
    request: Request,
    ctx: WorkspaceCtx = Depends(require_role(Role.VIEWER)),
    db: AsyncSession = Depends(get_db),
) -> AssistantOut:
    """Save a new assistant: a name, required instructions, and an optional model and tools."""
    assistant = await create_assistant(
        db,
        workspace_id=ctx.workspace_id,
        created_by=ctx.user,
        name=body.name,
        instructions=body.instructions,
        model_id=body.model_id,
        tool_ids=body.tool_ids,
        memory_enabled=body.memory_enabled,
    )
    await record_audit(
        db,
        actor_id=ctx.user.id,
        workspace_id=ctx.workspace_id,
        action="assistant.created",
        target_type="assistant",
        target_id=str(assistant.id),
        ip=client_ip(request),
        metadata={"name": assistant.name},
    )
    return await _to_assistant_out(db, assistant)


@router.get("", response_model=list[AssistantOut])
async def list_assistants_route(
    ctx: WorkspaceCtx = Depends(require_role(Role.VIEWER)),
    db: AsyncSession = Depends(get_db),
) -> list[AssistantOut]:
    """List every assistant saved in the workspace."""
    assistants = await list_assistants(db, workspace_id=ctx.workspace_id)
    return [await _to_assistant_out(db, a) for a in assistants]


@router.patch("/{assistant_id}", response_model=AssistantOut)
async def update_assistant_route(
    assistant_id: uuid.UUID,
    body: AssistantUpdate,
    request: Request,
    ctx: WorkspaceCtx = Depends(require_role(Role.VIEWER)),
    db: AsyncSession = Depends(get_db),
) -> AssistantOut:
    """Edit an assistant's fields or replace its assigned tools — only the fields sent change."""
    changes = body.model_dump(exclude_unset=True)
    assistant = await update_assistant(
        db, workspace_id=ctx.workspace_id, assistant_id=assistant_id, changes=changes
    )
    await record_audit(
        db,
        actor_id=ctx.user.id,
        workspace_id=ctx.workspace_id,
        action="assistant.updated",
        target_type="assistant",
        target_id=str(assistant.id),
        ip=client_ip(request),
        metadata={"changes": list(changes)},
    )
    return await _to_assistant_out(db, assistant)


@router.delete("/{assistant_id}", status_code=204)
async def delete_assistant_route(
    assistant_id: uuid.UUID,
    request: Request,
    ctx: WorkspaceCtx = Depends(require_role(Role.VIEWER)),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Permanently remove an assistant — conversations that used it fall back to plain chat."""
    await delete_assistant(db, workspace_id=ctx.workspace_id, assistant_id=assistant_id)
    await record_audit(
        db,
        actor_id=ctx.user.id,
        workspace_id=ctx.workspace_id,
        action="assistant.deleted",
        target_type="assistant",
        target_id=str(assistant_id),
        ip=client_ip(request),
    )
