"""One tool call made during a generation, and how it turned out — what makes a tool call visible
in the transcript after the fact, the same reason attachments are a table and not just text
folded into a message."""

import uuid
from typing import Any

from sqlalchemy import JSON, Enum, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.models.tool_invocation_status import ToolInvocationStatus


class ToolInvocation(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A single call the model made, and its result.

    `name` is denormalized from the tool it came from — kept even if that tool is later renamed
    or deleted (`tool_id` goes null via ON DELETE SET NULL), so history stays legible regardless
    of what happens to the tool afterward.
    """

    __tablename__ = "tool_invocations"

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    message_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"))
    tool_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tools.id", ondelete="SET NULL"), default=None
    )
    name: Mapped[str]
    arguments: Mapped[dict[str, Any]] = mapped_column(JSON)
    result: Mapped[str | None] = mapped_column(default=None)
    status: Mapped[ToolInvocationStatus] = mapped_column(
        Enum(ToolInvocationStatus, name="tool_invocation_status")
    )
    error: Mapped[str | None] = mapped_column(default=None)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
