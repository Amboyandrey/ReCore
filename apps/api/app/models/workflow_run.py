"""One execution of a workflow, end to end — the unit the run history page shows. `input` is the
run's own starting text (what `{{input}}` resolves to in every step's template); `output` is the
last step's output, once the run finishes. `current_position` is which step index the worker
should run (or resume, after an approval) next."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, Numeric
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.models.workflow_run_status import WorkflowRunStatus
from app.models.workflow_trigger import WorkflowTrigger


class WorkflowRun(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "workflow_runs"

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    workflow_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"))
    trigger: Mapped[WorkflowTrigger] = mapped_column(Enum(WorkflowTrigger, name="workflow_trigger"))
    status: Mapped[WorkflowRunStatus] = mapped_column(
        Enum(WorkflowRunStatus, name="workflow_run_status"), default=WorkflowRunStatus.QUEUED
    )
    input: Mapped[str]
    output: Mapped[str | None] = mapped_column(default=None)
    error: Mapped[str | None] = mapped_column(default=None)
    # None for a schedule or webhook trigger — nobody was "at the keyboard" to attribute it to.
    # The worker acts as started_by or, when unset, the workflow's own creator (see
    # workers/run_workflow.py) for flag evaluation, memory recall scope, and usage attribution.
    started_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    current_position: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    cost_usd: Mapped[float] = mapped_column(Numeric(10, 6, asdecimal=False), default=0, server_default="0")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
