"""Uploading a file into a conversation, extracting whatever text it holds, and reading it back.

Extraction is synchronous, on the request path, and covers what people actually attach: plain
text/markdown/JSON decoded directly, plus real PDF and DOCX parsing (pypdf and python-docx).
Anything else is stored but marked unsupported — "attached" and "extracted" are tracked
separately, so growing this list later (images via OCR, say) is an addition, not a schema change.
"""

import uuid
from io import BytesIO
from pathlib import Path

from docx import Document as DocxDocument
from pypdf import PdfReader
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import AttachmentNotFound, AttachmentTooLarge
from app.models import Attachment, ExtractStatus

settings = get_settings()

_TEXT_MIME_PREFIXES = ("text/",)
_TEXT_MIME_TYPES = {"application/json", "application/xml", "application/x-yaml"}
_PDF_MIME = "application/pdf"
_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# Caps how much of one file's text reaches the model — a single huge document otherwise crowds
# out the rest of the conversation's context budget entirely.
MAX_EXTRACTED_CHARS = 100_000


def _is_text_like(mime: str) -> bool:
    """Whether this is read as plain UTF-8 text, no parsing library involved."""
    return mime.startswith(_TEXT_MIME_PREFIXES) or mime in _TEXT_MIME_TYPES


def _cap(text: str) -> str:
    """Truncate extracted text at MAX_EXTRACTED_CHARS, with a visible marker that it happened."""
    if len(text) <= MAX_EXTRACTED_CHARS:
        return text
    return text[:MAX_EXTRACTED_CHARS] + "\n\n[...truncated]"


def _extract_plain_text(data: bytes) -> tuple[str | None, ExtractStatus, str | None]:
    """Text-like files are already text — just decode them."""
    try:
        return _cap(data.decode("utf-8")), ExtractStatus.DONE, None
    except UnicodeDecodeError as exc:
        return None, ExtractStatus.FAILED, str(exc)


def _extract_pdf_text(data: bytes) -> tuple[str | None, ExtractStatus, str | None]:
    """Concatenate every page's extracted text, page breaks marked with a blank line."""
    try:
        reader = PdfReader(BytesIO(data))
        text = "\n\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as exc:  # noqa: BLE001 — pypdf raises several distinct errors for bad PDFs
        return None, ExtractStatus.FAILED, str(exc)
    if not text.strip():
        return None, ExtractStatus.FAILED, "No extractable text found (the PDF may be scanned images)."
    return _cap(text), ExtractStatus.DONE, None


def _extract_docx_text(data: bytes) -> tuple[str | None, ExtractStatus, str | None]:
    """Join every paragraph's text — tables and headers/footers aren't walked, just the body."""
    try:
        document = DocxDocument(BytesIO(data))
        text = "\n".join(p.text for p in document.paragraphs)
    except Exception as exc:  # noqa: BLE001 — python-docx raises several distinct errors too
        return None, ExtractStatus.FAILED, str(exc)
    if not text.strip():
        return None, ExtractStatus.FAILED, "No extractable text found."
    return _cap(text), ExtractStatus.DONE, None


def _extract_text(data: bytes, mime: str) -> tuple[str | None, ExtractStatus, str | None]:
    """Dispatch to the right extractor for this mime type, or mark it unsupported."""
    if _is_text_like(mime):
        return _extract_plain_text(data)
    if mime == _PDF_MIME:
        return _extract_pdf_text(data)
    if mime == _DOCX_MIME:
        return _extract_docx_text(data)
    return None, ExtractStatus.UNSUPPORTED, None


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
