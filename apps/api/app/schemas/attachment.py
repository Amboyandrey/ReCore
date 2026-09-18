"""Request and response shapes for uploaded attachments."""

import uuid
from datetime import datetime

from pydantic import BaseModel

from app.models import ExtractStatus


class AttachmentOut(BaseModel):
    """An uploaded file — its extracted text, when extraction succeeded, but never raw bytes.
    Exactly one of `conversation_id` / `workflow_id` is set (see the Attachment model)."""

    id: uuid.UUID
    conversation_id: uuid.UUID | None
    message_id: uuid.UUID | None
    workflow_id: uuid.UUID | None
    workflow_run_id: uuid.UUID | None
    original_filename: str
    mime: str
    size: int
    extract_status: ExtractStatus
    extracted_text: str | None
    created_at: datetime
