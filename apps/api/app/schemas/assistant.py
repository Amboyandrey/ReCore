"""Request and response shapes for a workspace's saved assistants."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class AssistantCreate(BaseModel):
    """What creating an assistant needs. `instructions` are required — an assistant can't exist
    without them; `model_id` and `tool_ids` are both optional, and default to "none".
    `memory_enabled` turns on mem0-backed recall (see services/memory.py); it defaults off, same
    as every other optional capability an assistant can carry."""

    name: str = Field(min_length=1)
    instructions: str = Field(min_length=1)
    model_id: uuid.UUID | None = None
    tool_ids: list[uuid.UUID] = Field(default_factory=list)
    memory_enabled: bool = False
    delegate_ids: list[uuid.UUID] = Field(default_factory=list)
    connector_ids: list[uuid.UUID] = Field(default_factory=list)


class AssistantUpdate(BaseModel):
    """Only the fields actually sent are touched (built with `exclude_unset`) — so sending
    `model_id: null` explicitly clears an assistant's preferred model, and `tool_ids: []`
    explicitly unassigns every tool, while omitting either key leaves it as it was."""

    name: str | None = Field(default=None, min_length=1)
    instructions: str | None = Field(default=None, min_length=1)
    model_id: uuid.UUID | None = None
    tool_ids: list[uuid.UUID] | None = None
    memory_enabled: bool | None = None
    delegate_ids: list[uuid.UUID] | None = None
    connector_ids: list[uuid.UUID] | None = None


class AssistantOut(BaseModel):
    """One saved assistant, with the ids of the tools it's currently equipped with.

    `created_by` is exposed (unlike on most rows in this codebase) because it's load-bearing for
    the client: only the creator or the workspace owner may add or delete this assistant's
    curated memories (see services/memory.py), and the settings page needs to know which one it's
    looking at to show that control correctly rather than showing it to everyone and leaning on a
    403 to explain why.
    """

    id: uuid.UUID
    name: str
    instructions: str
    model_id: uuid.UUID | None
    tool_ids: list[uuid.UUID]
    memory_enabled: bool
    delegate_ids: list[uuid.UUID]
    connector_ids: list[uuid.UUID]
    created_by: uuid.UUID
    created_at: datetime
