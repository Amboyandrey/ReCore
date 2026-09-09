"""A saved name + required instructions + optional model — chatting with an assistant means the
model arrives already briefed, and optionally already equipped with tools (see AssistantTool)."""

import uuid

from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class Assistant(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One workspace's saved assistant.

    `instructions` are required — an assistant can't exist without them — and become the system
    turn for any conversation using it, replacing that conversation's own `system_prompt`.
    `model_id` is optional: a new conversation started with this assistant pre-fills its model
    picker from it, falling back to the composer's usual default when it's unset. Both it and the
    assistant's assigned tools (see AssistantTool) are resolved live wherever a chat reads them,
    never copied onto a conversation — so retargeting an assistant's model, instructions, or tools
    later reaches every conversation still using it, not just new ones.
    """

    __tablename__ = "assistants"

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    name: Mapped[str]
    instructions: Mapped[str]
    model_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("models.id", ondelete="SET NULL"), default=None
    )
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
