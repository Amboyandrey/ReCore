"""The memory layer's HTTP surface — gated behind the `memory` flag throughout.

Two routers, same split invitations.py uses for its own two prefixes: `credential_router` for
the workspace's mem0 key (admin-only, matching provider credentials), and `memories_router` for
one assistant's curated and personal memories (any member may view; adding or deleting a curated
one is restricted to its creator or the workspace owner — see services/memory.py).
"""

import uuid

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.request_ip import client_ip
from app.deps.flags import flag_gate
from app.deps.workspace import WorkspaceCtx
from app.models import Role
from app.schemas.memory import (
    CuratedMemoryCreate,
    MemoryCredentialOut,
    MemoryCredentialSet,
    MemoryOut,
    MemoryQueuedOut,
    MemoryScope,
)
from app.services.audit import record_audit
from app.services.memory import (
    add_curated,
    delete_credential,
    delete_memory,
    has_credential,
    list_curated,
    list_personal,
    set_credential,
)

credential_router = APIRouter(prefix="/workspaces/{workspace_id}/memory", tags=["memory"])
memories_router = APIRouter(
    prefix="/workspaces/{workspace_id}/assistants/{assistant_id}/memories", tags=["memory"]
)


def _to_memory_out(row: dict[str, object]) -> MemoryOut:
    return MemoryOut(
        id=str(row.get("id")),
        memory=str(row.get("memory", "")),
        created_at=str(row["created_at"]) if row.get("created_at") is not None else None,
    )


@credential_router.put("/credential", response_model=MemoryCredentialOut)
async def set_credential_route(
    body: MemoryCredentialSet,
    request: Request,
    ctx: WorkspaceCtx = Depends(flag_gate("memory", minimum=Role.ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> MemoryCredentialOut:
    """Set or rotate the workspace's mem0 API key."""
    await set_credential(db, workspace_id=ctx.workspace_id, created_by=ctx.user, api_key=body.api_key)
    await record_audit(
        db,
        actor_id=ctx.user.id,
        workspace_id=ctx.workspace_id,
        action="memory.credential_set",
        target_type="workspace",
        target_id=str(ctx.workspace_id),
        ip=client_ip(request),
    )
    return MemoryCredentialOut(has_key=True)


@credential_router.get("/credential", response_model=MemoryCredentialOut)
async def get_credential_route(
    ctx: WorkspaceCtx = Depends(flag_gate("memory", minimum=Role.ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> MemoryCredentialOut:
    """Report whether the workspace has a mem0 key configured — never the key itself."""
    return MemoryCredentialOut(has_key=await has_credential(db, workspace_id=ctx.workspace_id))


@credential_router.delete("/credential", status_code=204)
async def delete_credential_route(
    request: Request,
    ctx: WorkspaceCtx = Depends(flag_gate("memory", minimum=Role.ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Remove the workspace's mem0 key — memories already in mem0 aren't deleted by this."""
    await delete_credential(db, workspace_id=ctx.workspace_id)
    await record_audit(
        db,
        actor_id=ctx.user.id,
        workspace_id=ctx.workspace_id,
        action="memory.credential_deleted",
        target_type="workspace",
        target_id=str(ctx.workspace_id),
        ip=client_ip(request),
    )


@memories_router.get("", response_model=list[MemoryOut])
async def list_memories_route(
    assistant_id: uuid.UUID,
    scope: MemoryScope = Query(...),
    ctx: WorkspaceCtx = Depends(flag_gate("memory", minimum=Role.VIEWER)),
    db: AsyncSession = Depends(get_db),
) -> list[MemoryOut]:
    """List an assistant's curated memories (visible to any member) or the caller's own personal
    ones (never anyone else's — `scope=personal` always means *this* caller)."""
    if scope == "curated":
        rows = await list_curated(db, workspace_id=ctx.workspace_id, assistant_id=assistant_id)
    else:
        rows = await list_personal(
            db, workspace_id=ctx.workspace_id, assistant_id=assistant_id, user_id=ctx.user.id
        )
    return [_to_memory_out(r) for r in rows]


@memories_router.post("", status_code=202, response_model=MemoryQueuedOut)
async def add_curated_memory_route(
    assistant_id: uuid.UUID,
    body: CuratedMemoryCreate,
    request: Request,
    ctx: WorkspaceCtx = Depends(flag_gate("memory", minimum=Role.VIEWER)),
    db: AsyncSession = Depends(get_db),
) -> MemoryQueuedOut:
    """Teach an assistant a curated fact — restricted to its creator or the workspace owner.
    mem0 processes this asynchronously, so a 202 here means queued, not yet listable."""
    await add_curated(
        db,
        workspace_id=ctx.workspace_id,
        assistant_id=assistant_id,
        caller_id=ctx.user.id,
        is_owner=ctx.role == Role.OWNER,
        text=body.text,
    )
    await record_audit(
        db,
        actor_id=ctx.user.id,
        workspace_id=ctx.workspace_id,
        action="memory.curated_added",
        target_type="assistant",
        target_id=str(assistant_id),
        ip=client_ip(request),
    )
    return MemoryQueuedOut()


@memories_router.delete("/{memory_id}", status_code=204)
async def delete_memory_route(
    assistant_id: uuid.UUID,
    memory_id: str,
    request: Request,
    scope: MemoryScope = Query(...),
    ctx: WorkspaceCtx = Depends(flag_gate("memory", minimum=Role.VIEWER)),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete one memory. A curated one requires being the assistant's creator or the workspace
    owner; a personal one is always the caller's own — see services/memory.py for how a caller
    can never delete a memory outside the scope they're authorized for, even by guessing its id.
    """
    await delete_memory(
        db,
        workspace_id=ctx.workspace_id,
        assistant_id=assistant_id,
        memory_id=memory_id,
        scope=scope,
        caller_id=ctx.user.id,
        is_owner=ctx.role == Role.OWNER,
    )
    await record_audit(
        db,
        actor_id=ctx.user.id,
        workspace_id=ctx.workspace_id,
        action="memory.memory_deleted",
        target_type="assistant",
        target_id=str(assistant_id),
        ip=client_ip(request),
        metadata={"scope": scope, "memory_id": memory_id},
    )
