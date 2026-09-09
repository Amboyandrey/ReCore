"""Request and response shapes for the tools a chat can call. The secret a tool holds (an API
key, a header value) never appears in any response — only in the request that sets it."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.models import ToolInvocationStatus, ToolKind


class WebSearchEnable(BaseModel):
    """The one thing turning on the built-in web search needs — its Tavily key."""

    api_key: str = Field(min_length=1)


class ToolOut(BaseModel):
    """A tool as the settings page shows it."""

    id: uuid.UUID
    name: str
    description: str
    kind: ToolKind
    enabled: bool
    created_at: datetime


class ToolInvocationOut(BaseModel):
    """One tool call made during a conversation, as the chat thread shows it — no secrets ride
    along here, just what was asked and what came back."""

    id: uuid.UUID
    message_id: uuid.UUID
    tool_id: uuid.UUID | None
    name: str
    arguments: dict[str, Any]
    result: str | None
    status: ToolInvocationStatus
    error: str | None
    created_at: datetime
