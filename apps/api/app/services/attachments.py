"""Uploading a file into a conversation, extracting whatever text it holds, and reading it back.

Extraction is synchronous and deliberately simple: text-like files are decoded as UTF-8 and used
as-is; anything else is stored but marked unsupported. Parsing PDFs or images is a real feature,
not a corner of this one — "attached" and "extracted" are tracked separately so it can grow into
that later without a schema change.
"""

import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import AttachmentNotFound, AttachmentTooLarge
from app.models import Attachment, ExtractStatus

settings = get_settings()

_TEXT_MIME_PREFIXES = ("text/",)
_TEXT_MIME_TYPES = {"application/json", "application/xml", "application/x-yaml"}


def _is_text_like(mime: str) -> bool:
    """Whether this phase knows how to pull text out of this mime type."""
    return mime.startswith(_TEXT_MIME_PREFIXES) or mime in _TEXT_MIME_TYPES


def _extract_text(data: bytes, mime: str) -> tuple[str | None, ExtractStatus, str | None]:
    """Best-effort text extraction: decode as UTF-8 for anything text-like, skip everything else."""
    if not _is_text_like(mime):
        return None, ExtractStatus.UNSUPPORTED, None
    try:
        return data.decode("utf-8"), ExtractStatus.DONE, None
    except UnicodeDecodeError as exc:
        return None, ExtractStatus.FAILED, str(exc)


async def save_attachment(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    conversation_id: uuid.UUID,
    uploaded_by: uuid.UUID,
    filename: str,
    mime: str,
    data: bytes,
) -> Attachment:
    """Write an uploaded file to disk under the workspace, extract its text, and record both."""
    if len(data) > settings.max_attachment_size_bytes:
        raise AttachmentTooLarge()

    attachment_id = uuid.uuid4()
    storage_key = f"{workspace_id}/{attachment_id}"
    path = Path(settings.storage_dir) / storage_key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)

    extracted_text, extract_status, extract_error = _extract_text(data, mime)
    attachment = Attachment(
        id=attachment_id,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        uploaded_by=uploaded_by,
        original_filename=filename,
        mime=mime,
        size=len(data),
        storage_key=storage_key,
        extracted_text=extracted_text,
        extract_status=extract_status,
        extract_error=extract_error,
    )
    db.add(attachment)
    await db.flush()
    return attachment


async def list_attachments(db: AsyncSession, *, conversation_id: uuid.UUID) -> list[Attachment]:
    """List every attachment uploaded into a conversation, oldest first."""
    stmt = (
        select(Attachment)
        .where(Attachment.conversation_id == conversation_id)
        .order_by(Attachment.created_at)
    )
    return list((await db.scalars(stmt)).all())


async def get_attachment(
    db: AsyncSession, *, conversation_id: uuid.UUID, attachment_id: uuid.UUID
) -> Attachment:
    """Load one attachment by id, scoped to its conversation, or raise if it isn't there."""
    attachment = await db.scalar(
        select(Attachment).where(
            Attachment.id == attachment_id, Attachment.conversation_id == conversation_id
        )
    )
    if attachment is None:
        raise AttachmentNotFound()
    return attachment


async def attach_to_message(
    db: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    attachment_ids: list[uuid.UUID],
    message_id: uuid.UUID,
) -> list[Attachment]:
    """Link already-uploaded attachments to the message that was just sent alongside them."""
    attachments = []
    for attachment_id in attachment_ids:
        attachment = await get_attachment(
            db, conversation_id=conversation_id, attachment_id=attachment_id
        )
        attachment.message_id = message_id
        attachments.append(attachment)
    await db.flush()
    return attachments
