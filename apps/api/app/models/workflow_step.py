"""One step in a workflow's chain — an assistant plus the prompt template it's given. `key` is
how a later step's template refers back to this one's output (`{{steps.<key>.output}}`), and
`position` is the order steps run in — both unique per workflow (see the migration)."""

import uuid

from sqlalchemy import Boolean, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class WorkflowStep(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "workflow_steps"

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    workflow_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer)
    key: Mapped[str]
    name: Mapped[str]
    assistant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("assistants.id", ondelete="SET NULL"), default=None
    )
    prompt_template: Mapped[str]
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
