"""One step's own execution within a run — snapshots the step's key/name/assistant/template at
the moment the run started, so a later edit to the workflow's steps (which replaces every
WorkflowStep row wholesale — see services/workflows.py's _set_workflow_steps) never rewrites, or
strands, a run already in flight: `step_id` may go null (ON DELETE SET NULL) the moment someone
resaves the workflow's steps while this run is queued or running, but `prompt_template` still
needs to be there for the worker to render this step's prompt from. `prompt` is the *rendered*
result, filled in once the worker actually gets to this step — kept separately so a run's history
always shows exactly what the assistant was actually given, not just the template that produced it.

`invocations`/`sources` hold the same shape ToolInvocation rows and MessageSource rows carry in
chat, but inline as JSON rather than their own tables — a step run isn't a chat Message, so there
is no message_id for either of those tables' own foreign keys to point at.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.models.workflow_step_status import WorkflowStepStatus


class WorkflowStepRun(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "workflow_step_runs"

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workflow_runs.id", ondelete="CASCADE"))
    step_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workflow_steps.id", ondelete="SET NULL"), default=None
    )
    position: Mapped[int] = mapped_column(Integer)
    key: Mapped[str]
    name: Mapped[str]
    assistant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("assistants.id", ondelete="SET NULL"), default=None
    )
    prompt_template: Mapped[str]
    requires_approval: Mapped[bool] = mapped_column(default=False, server_default="false")
    status: Mapped[WorkflowStepStatus] = mapped_column(
        Enum(WorkflowStepStatus, name="workflow_step_status"), default=WorkflowStepStatus.PENDING
    )
    prompt: Mapped[str | None] = mapped_column(String, default=None)
    output: Mapped[str | None] = mapped_column(String, default=None)
    error: Mapped[str | None] = mapped_column(default=None)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    tokens_out: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    cost_usd: Mapped[float] = mapped_column(Numeric(10, 6, asdecimal=False), default=0, server_default="0")
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    invocations: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, server_default="[]")
    sources: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, server_default="[]")
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
