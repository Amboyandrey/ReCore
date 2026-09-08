"""An uploaded file, and whatever text could be pulled out of it for the model to read."""

import uuid

from sqlalchemy import Enum, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.extract_status import ExtractStatus
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class Attachment(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A file uploaded into a conversation, gated behind the `attachments` flag.

    `message_id` is null until the attachment is actually sent with a message — a file can be
    uploaded ahead of the send that references it, same as most chat products let you attach
    before you hit send.
    """

    __tablename__ = "attachments"

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE")
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL"), default=None
    )
    uploaded_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    original_filename: Mapped[str]
    mime: Mapped[str]
    size: Mapped[int] = mapped_column(Integer)
    storage_key: Mapped[str]
    extracted_text: Mapped[str | None] = mapped_column(default=None)
    extract_status: Mapped[ExtractStatus] = mapped_column(
        Enum(ExtractStatus, name="extract_status"), default=ExtractStatus.PENDING
    )
    extract_error: Mapped[str | None] = mapped_column(default=None)
