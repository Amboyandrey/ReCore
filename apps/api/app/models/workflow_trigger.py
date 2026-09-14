"""What started a workflow run."""

import enum


class WorkflowTrigger(enum.StrEnum):
    MANUAL = "manual"
    SCHEDULE = "schedule"
    WEBHOOK = "webhook"
