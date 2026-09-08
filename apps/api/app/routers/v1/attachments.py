"""Upload a file into a conversation and list what's there — gated behind the `attachments` flag."""

import uuid

from fastapi import APIRouter, Depends, File, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.deps.flags import flag_gate
from app.deps.workspace import WorkspaceCtx
from app.models import Role
from app.schemas.attachment import AttachmentOut
from app.services.attachments import list_attachments, save_attachment
from app.services.chat import get_conversation

router = APIRouter(
    prefix="/workspaces/{workspace_id}/conversations/{conversation_id}/attachments",
    tags=["attachments"],
)


@router.post("", status_code=201, response_model=AttachmentOut)
async def upload_attachment_route(
    conversation_id: uuid.UUID,
    file: UploadFile = File(...),
    ctx: WorkspaceCtx = Depends(flag_gate("attachments", minimum=Role.VIEWER)),
    db: AsyncSession = Depends(get_db),
) -> AttachmentOut:
    """Upload a file into a conversation — 404s entirely while the `attachments` flag is off."""
    await get_conversation(db, workspace_id=ctx.workspace_id, conversation_id=conversation_id)
    data = await file.read()
    attachment = await save_attachment(
        db,
        workspace_id=ctx.workspace_id,
        conversation_id=conversation_id,
        uploaded_by=ctx.user.id,
        filename=file.filename or "upload",
        mime=file.content_type or "application/octet-stream",
        data=data,
    )
    return AttachmentOut.model_validate(attachment, from_attributes=True)


@router.get("", response_model=list[AttachmentOut])
async def list_attachments_route(
    conversation_id: uuid.UUID,
    ctx: WorkspaceCtx = Depends(flag_gate("attachments", minimum=Role.VIEWER)),
    db: AsyncSession = Depends(get_db),
) -> list[AttachmentOut]:
    """List every attachment uploaded into a conversation."""
    await get_conversation(db, workspace_id=ctx.workspace_id, conversation_id=conversation_id)
    attachments = await list_attachments(db, conversation_id=conversation_id)
    return [AttachmentOut.model_validate(a, from_attributes=True) for a in attachments]
