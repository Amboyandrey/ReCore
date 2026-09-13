"""Which other assistants an assistant may delegate a task to during chat — a self-referential
many-to-many join, same shape as `assistant_tools`."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class AssistantDelegate(Base):
    """One (assistant, delegate) pairing. `assistant_id` is the orchestrator offered the
    `ask_<delegate>` tool; `delegate_id` is who answers it. The check constraint makes
    self-delegation impossible at the database level, not just in the service layer."""

    __tablename__ = "assistant_delegates"
    __table_args__ = (CheckConstraint("assistant_id <> delegate_id", name="ck_assistant_delegates_not_self"),)

    assistant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assistants.id", ondelete="CASCADE"), primary_key=True
    )
    delegate_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assistants.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
