"""Conversations, messages, and the SSE endpoints chat streaming runs through.

Sending a message and resuming a generation both return the same shape: a stream of
`id: ...\\nevent: ...\\ndata: ...` blocks. The `id:` line lets a browser's native EventSource
supply `Last-Event-ID` automatically on a network-level reconnect; a full page reload has no
surviving JS state to do that itself, so the frontend instead checks the active-generation
endpoint below and opens a fresh resume connection with `?after=` if there's something to catch
up on — see ARCHITECTURE.md's resumable-streaming design.
"""

import json
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import StreamingResponse
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.redis import get_redis
from app.core.request_ip import client_ip
from app.deps.workspace import WorkspaceCtx, require_role
from app.models import Role
from app.schemas.chat import (
    ActiveGenerationOut,
    ConversationCreate,
    ConversationOut,
    ConversationUpdate,
    MessageOut,
    SendMessageRequest,
)
from app.services.audit import record_audit
from app.services.chat import (
    create_conversation,
    delete_conversation,
    get_conversation,
    list_conversations,
    list_messages,
    send_message,
    update_conversation,
)
from app.services.generations import get_active_generation, read_events, request_stop

router = APIRouter(prefix="/workspaces/{workspace_id}/conversations", tags=["chat"])

_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


async def _sse_body(redis: Redis, generation_id: str, *, after: str = "0") -> AsyncIterator[bytes]:
    """Format a generation's events as SSE — one `id:`/`event:`/`data:` block per event."""
    async for event in read_events(redis, generation_id, after=after):
        yield f"id: {event.id}\nevent: {event.type}\ndata: {json.dumps(event.data)}\n\n".encode()


@router.post("", status_code=201, response_model=ConversationOut)
async def create_conversation_route(
    body: ConversationCreate,
    ctx: WorkspaceCtx = Depends(require_role(Role.VIEWER)),
    db: AsyncSession = Depends(get_db),
) -> ConversationOut:
    """Start a new conversation pinned to one of the workspace's enabled models."""
    conversation = await create_conversation(
        db,
        workspace_id=ctx.workspace_id,
        user=ctx.user,
        model_id=body.model_id,
        system_prompt=body.system_prompt,
    )
    return ConversationOut.model_validate(conversation, from_attributes=True)


@router.get("", response_model=list[ConversationOut])
async def list_conversations_route(
    ctx: WorkspaceCtx = Depends(require_role(Role.VIEWER)),
    db: AsyncSession = Depends(get_db),
) -> list[ConversationOut]:
    """List every conversation this caller can see: their own, plus any of the workspace's
    conversations that have been shared."""
    conversations = await list_conversations(db, workspace_id=ctx.workspace_id, viewer_id=ctx.user.id)
    return [ConversationOut.model_validate(c, from_attributes=True) for c in conversations]


@router.get("/{conversation_id}", response_model=ConversationOut)
async def get_conversation_route(
    conversation_id: uuid.UUID,
    ctx: WorkspaceCtx = Depends(require_role(Role.VIEWER)),
    db: AsyncSession = Depends(get_db),
) -> ConversationOut:
    """Fetch one conversation — 404s the same way for one that doesn't exist and one that's
    private to someone else."""
    conversation = await get_conversation(
        db, workspace_id=ctx.workspace_id, conversation_id=conversation_id, viewer_id=ctx.user.id
    )
    return ConversationOut.model_validate(conversation, from_attributes=True)


@router.patch("/{conversation_id}", response_model=ConversationOut)
async def update_conversation_route(
    conversation_id: uuid.UUID,
    body: ConversationUpdate,
    ctx: WorkspaceCtx = Depends(require_role(Role.VIEWER)),
    db: AsyncSession = Depends(get_db),
) -> ConversationOut:
    """Switch a conversation's model and/or toggle its sharing — only the owner may share/unshare."""
    changes = body.model_dump(exclude_unset=True)
    conversation = await update_conversation(
        db,
        workspace_id=ctx.workspace_id,
        conversation_id=conversation_id,
        viewer_id=ctx.user.id,
        model_id=changes.get("model_id"),
        shared=changes.get("shared"),
    )
    return ConversationOut.model_validate(conversation, from_attributes=True)


@router.delete("/{conversation_id}", status_code=204)
async def delete_conversation_route(
    conversation_id: uuid.UUID,
    request: Request,
    ctx: WorkspaceCtx = Depends(require_role(Role.VIEWER)),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Permanently delete a conversation, its messages, attachments, and usage history — only
    the conversation's own owner may, even if it's been shared."""
    await delete_conversation(
        db, workspace_id=ctx.workspace_id, conversation_id=conversation_id, viewer_id=ctx.user.id
    )
    await record_audit(
        db,
        actor_id=ctx.user.id,
        workspace_id=ctx.workspace_id,
        action="conversation.deleted",
        target_type="conversation",
        target_id=str(conversation_id),
        ip=client_ip(request),
    )


@router.get("/{conversation_id}/messages", response_model=list[MessageOut])
async def list_messages_route(
    conversation_id: uuid.UUID,
    ctx: WorkspaceCtx = Depends(require_role(Role.VIEWER)),
    db: AsyncSession = Depends(get_db),
) -> list[MessageOut]:
    """List a conversation's messages, oldest first."""
    conversation = await get_conversation(
        db, workspace_id=ctx.workspace_id, conversation_id=conversation_id, viewer_id=ctx.user.id
    )
    messages = await list_messages(db, conversation_id=conversation.id)
    return [MessageOut.model_validate(m, from_attributes=True) for m in messages]


@router.get("/{conversation_id}/active-generation", response_model=ActiveGenerationOut)
async def get_active_generation_route(
    conversation_id: uuid.UUID,
    ctx: WorkspaceCtx = Depends(require_role(Role.VIEWER)),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> ActiveGenerationOut:
    """Report whether a generation is currently running — for a freshly loaded page to know
    whether there's a reply already in progress to resume."""
    await get_conversation(
        db, workspace_id=ctx.workspace_id, conversation_id=conversation_id, viewer_id=ctx.user.id
    )
    generation_id = await get_active_generation(redis, conversation_id)
    return ActiveGenerationOut(generation_id=generation_id)


@router.post("/{conversation_id}/messages")
async def send_message_route(
    conversation_id: uuid.UUID,
    body: SendMessageRequest,
    ctx: WorkspaceCtx = Depends(require_role(Role.VIEWER)),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> StreamingResponse:
    """Send a message and stream the reply back as SSE."""
    conversation = await get_conversation(
        db, workspace_id=ctx.workspace_id, conversation_id=conversation_id, viewer_id=ctx.user.id
    )
    generation_id = await send_message(
        db,
        redis,
        workspace_id=ctx.workspace_id,
        conversation=conversation,
        content=body.content,
        idempotency_key=idempotency_key,
        attachment_ids=body.attachment_ids,
    )
    return StreamingResponse(
        _sse_body(redis, generation_id), media_type="text/event-stream", headers=_SSE_HEADERS
    )


@router.get("/{conversation_id}/generations/{generation_id}")
async def resume_generation_route(
    conversation_id: uuid.UUID,
    generation_id: str,
    request: Request,
    ctx: WorkspaceCtx = Depends(require_role(Role.VIEWER)),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> StreamingResponse:
    """Resume tailing a generation, from its Last-Event-ID header or an explicit ?after= param."""
    await get_conversation(
        db, workspace_id=ctx.workspace_id, conversation_id=conversation_id, viewer_id=ctx.user.id
    )
    after = request.headers.get("last-event-id") or request.query_params.get("after") or "0"
    return StreamingResponse(
        _sse_body(redis, generation_id, after=after),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@router.post("/{conversation_id}/generations/{generation_id}/stop", status_code=204)
async def stop_generation_route(
    conversation_id: uuid.UUID,
    generation_id: str,
    ctx: WorkspaceCtx = Depends(require_role(Role.VIEWER)),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> None:
    """Signal a running generation to stop."""
    await get_conversation(
        db, workspace_id=ctx.workspace_id, conversation_id=conversation_id, viewer_id=ctx.user.id
    )
    await request_stop(redis, generation_id)
