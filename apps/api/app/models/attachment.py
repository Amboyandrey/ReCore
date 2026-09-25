"""An uploaded file, and whatever text could be pulled out of it for the model to read."""

import uuid

from sqlalchemy import Enum, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.attachment_source import AttachmentSource
from app.models.extract_status import ExtractStatus
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class Attachment(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A file uploaded into a conversation (gated behind the `attachments` flag) or into a
    workflow as a run's input (gated behind `workflows`) — exactly one of `conversation_id` /
    `workflow_id` is set, enforced by a CHECK constraint.

    `message_id` is null until the attachment is actually sent with a message — a file can be
    uploaded ahead of the send that references it, same as most chat products let you attach
    before you hit send. `workflow_run_id` plays the same role for a workflow upload: null until
    the run it was uploaded for starts.

    A `TOOL` attachment is an image a tool call returned: it's saved on the assistant reply along
    with `tool_invocation_id`, and `uploaded_by` is the member whose chat made the call.
    """

    __tablename__ = "attachments"

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), default=None
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL"), default=None
    )
    workflow_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workflows.id", ondelete="CASCADE"), default=None
    )
    workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="CASCADE"), default=None
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
    source: Mapped[AttachmentSource] = mapped_column(
        Enum(AttachmentSource, name="attachment_source"), default=AttachmentSource.UPLOAD
    )
    tool_invocation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tool_invocations.id", ondelete="CASCADE"), default=None
    )
