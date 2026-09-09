"""Request and response shapes for a workspace's saved assistants."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class AssistantCreate(BaseModel):
    """What creating an assistant needs. `instructions` are required — an assistant can't exist
    without them; `model_id` and `tool_ids` are both optional, and default to "none"."""

    name: str = Field(min_length=1)
    instructions: str = Field(min_length=1)
    model_id: uuid.UUID | None = None
    tool_ids: list[uuid.UUID] = Field(default_factory=list)


class AssistantUpdate(BaseModel):
    """Only the fields actually sent are touched (built with `exclude_unset`) — so sending
    `model_id: null` explicitly clears an assistant's preferred model, and `tool_ids: []`
    explicitly unassigns every tool, while omitting either key leaves it as it was."""

    name: str | None = Field(default=None, min_length=1)
    instructions: str | None = Field(default=None, min_length=1)
    model_id: uuid.UUID | None = None
    tool_ids: list[uuid.UUID] | None = None


class AssistantOut(BaseModel):
    """One saved assistant, with the ids of the tools it's currently equipped with."""

    id: uuid.UUID
    name: str
    instructions: str
    model_id: uuid.UUID | None
    tool_ids: list[uuid.UUID]
    created_at: datetime
