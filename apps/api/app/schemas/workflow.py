"""Request and response shapes for a workspace's saved workflows and their runs."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.models import WorkflowRunStatus, WorkflowStepStatus, WorkflowTrigger


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
    created_by: uuid.UUID
    created_at: datetime


class RunCreate(BaseModel):
    """What starting a run needs — the text every step's `{{input}}` placeholder resolves to."""

    input: str = Field(min_length=1)


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
