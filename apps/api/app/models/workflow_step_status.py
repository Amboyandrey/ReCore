"""One step run's own lifecycle, independent of (but driven by) its parent WorkflowRunStatus."""

import enum


class WorkflowStepStatus(enum.StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
