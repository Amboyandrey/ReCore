"""A chat thread — scoped to a workspace, pinned to one model at a time."""

import uuid

from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class Conversation(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One chat thread. `title` starts as a heuristic from the first message, editable later.

    Private to `user_id` by default — every other workspace member is blocked from even seeing
    it exists (see get_conversation/list_conversations) until its owner flips `shared` on, at
    which point it becomes visible (and, since nothing else gates sending a message into a
    conversation you can see, writable) to the rest of the workspace too.
    """

    __tablename__ = "conversations"

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    model_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("models.id"))
    title: Mapped[str]
    system_prompt: Mapped[str | None] = mapped_column(default=None)
    shared: Mapped[bool] = mapped_column(default=False, server_default="false")
