"""Request and response shapes for a workspace's saved workflows and their runs."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator

from app.models import ExtractStatus, WorkflowRunStatus, WorkflowStepStatus, WorkflowTrigger


class WorkflowStepIn(BaseModel):
    """One step as sent in a create/update body. `key` identifies it for
    `{{steps.<key>.output}}` references in later steps — see app/workflows/template.py."""

    key: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1)
    assistant_id: uuid.UUID
    prompt_template: str = Field(min_length=1)
    requires_approval: bool = False


class WorkflowCreate(BaseModel):
    name: str = Field(min_length=1)
    description: str = ""
    default_model_id: uuid.UUID | None = None
    steps: list[WorkflowStepIn] = Field(min_length=1)


class WorkflowUpdate(BaseModel):
    """Only the fields actually sent are touched (built with `exclude_unset`). Sending `steps`
    always replaces the whole chain, same as `tool_ids` already does on an assistant."""

    name: str | None = Field(default=None, min_length=1)
    description: str | None = None
    enabled: bool | None = None
    default_model_id: uuid.UUID | None = None
    steps: list[WorkflowStepIn] | None = Field(default=None, min_length=1)


class WorkflowStepOut(BaseModel):
    id: uuid.UUID
    key: str
    name: str
    assistant_id: uuid.UUID | None
    prompt_template: str
    requires_approval: bool


class WorkflowOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    enabled: bool
    default_model_id: uuid.UUID | None
    steps: list[WorkflowStepOut]
    # None means the inbound webhook is off. The secret is the credential — it's what the hook
    # URL embeds (see routers/v1/hooks.py) — so it's only ever shown to workspace members.
    webhook_secret: str | None
    created_by: uuid.UUID
    created_at: datetime


class WebhookOut(BaseModel):
    """What enabling (or rotating) a workflow's webhook returns — the fresh secret its URL embeds."""

    webhook_secret: str


class RunCreate(BaseModel):
    """What starting a run needs — the text every step's `{{input}}` placeholder resolves to,
    and/or files already uploaded into the workflow. At least one of the two must be given."""

    input: str = ""
    attachment_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def _text_or_files(self) -> "RunCreate":
        """A run has to start from *something* — empty text with no files is a no-op, not a run."""
        if not self.input.strip() and not self.attachment_ids:
            raise ValueError("Give the run some input text, at least one attachment, or both.")
        return self


class RunAttachmentOut(BaseModel):
    """One file a run was started with — just enough to list it; the text is inside the step's
    prompt already."""

    id: uuid.UUID
    original_filename: str
    mime: str
    size: int
    extract_status: ExtractStatus


class WorkflowStepRunOut(BaseModel):
    id: uuid.UUID
    step_id: uuid.UUID | None
    position: int
    key: str
    name: str
    assistant_id: uuid.UUID | None
    status: WorkflowStepStatus
    prompt: str | None
    output: str | None
    error: str | None
    tokens_in: int
    tokens_out: int
    cost_usd: float
    latency_ms: int
    invocations: list[dict[str, Any]]
    sources: list[dict[str, Any]]
    approved_by: uuid.UUID | None
    approved_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None


class WorkflowRunOut(BaseModel):
    id: uuid.UUID
    workflow_id: uuid.UUID
    trigger: WorkflowTrigger
    status: WorkflowRunStatus
    input: str
    attachments: list[RunAttachmentOut]
    output: str | None
    error: str | None
    started_by: uuid.UUID | None
    current_position: int
    cost_usd: float
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime


class WorkflowRunDetailOut(WorkflowRunOut):
    steps: list[WorkflowStepRunOut]
