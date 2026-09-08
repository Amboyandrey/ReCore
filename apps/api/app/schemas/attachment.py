"""Request and response shapes for uploaded attachments."""

import uuid
from datetime import datetime

from pydantic import BaseModel

from app.models import ExtractStatus


class AttachmentOut(BaseModel):
    """An uploaded file — its extracted text, when extraction succeeded, but never raw bytes."""

    id: uuid.UUID
    conversation_id: uuid.UUID
    message_id: uuid.UUID | None
    original_filename: str
    mime: str
    size: int
    extract_status: ExtractStatus
    extracted_text: str | None
    created_at: datetime
