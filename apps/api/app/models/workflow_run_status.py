"""A workflow run's lifecycle: QUEUED (enqueued, not yet picked up) -> RUNNING -> either
SUCCEEDED, FAILED, CANCELED, or (a step with requires_approval) WAITING_APPROVAL, which itself
resolves to RUNNING again (approved) or REJECTED."""

import enum


class WorkflowRunStatus(enum.StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"
    CANCELED = "canceled"
