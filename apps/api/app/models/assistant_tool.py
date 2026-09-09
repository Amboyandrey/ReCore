"""Which tools an assistant is equipped with for chat — a many-to-many join, since a tool can be
assigned to several assistants and an assistant's tools are entirely optional."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class AssistantTool(Base):
    """One (assistant, tool) pairing — the composite key an assignment can only exist once as,
    same shape WorkspaceMember uses for a user's membership in a workspace."""

    __tablename__ = "assistant_tools"

    assistant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assistants.id", ondelete="CASCADE"), primary_key=True
    )
    tool_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tools.id", ondelete="CASCADE"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
