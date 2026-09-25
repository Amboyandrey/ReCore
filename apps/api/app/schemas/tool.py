"""Request and response shapes for the tools a chat can call. The secret a tool holds (an API
key, a header value) never appears in any response — only in the request that sets it."""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.models import ToolInvocationStatus, ToolKind

# OpenAI's own function-name constraint — the strictest of the three providers' — adopted here so
# a tool's name is valid on whichever one ends up calling it.
_TOOL_NAME_PATTERN = r"^[a-zA-Z0-9_-]+$"
HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]


class WebSearchEnable(BaseModel):
    """The one thing turning on the built-in web search needs — its Tavily key."""

    api_key: str = Field(min_length=1)


class HttpToolCreate(BaseModel):
    """Registering a third-party tool: what to call it, what it does, its JSON Schema
    parameters, and where/how to reach it.

    `secret_value`, if given, is sent as the `secret_header` header on every call — encrypted at
    rest the same way a provider credential's key is, and never returned in any response after
    this request.
    """

    name: str = Field(min_length=1, max_length=64, pattern=_TOOL_NAME_PATTERN)
    description: str = Field(min_length=1)
    parameters: dict[str, Any]
    method: HttpMethod
    url: str = Field(min_length=1)
    secret_header: str | None = Field(default=None, min_length=1)
    secret_value: str | None = Field(default=None, min_length=1)


class ToolUpdate(BaseModel):
    """Changing a tool after it's registered — only the fields actually sent are touched (built
    with `exclude_unset`).

    `enabled` is how any tool, built-in or HTTP, is turned on or off without losing its
    configuration; every other field only ever applies to an HTTP tool. Sending `secret_value`
    rotates the stored secret; omitting it leaves whatever's already there untouched.
    """

    enabled: bool | None = None
    name: str | None = Field(default=None, min_length=1, max_length=64, pattern=_TOOL_NAME_PATTERN)
    description: str | None = Field(default=None, min_length=1)
    parameters: dict[str, Any] | None = None
    method: HttpMethod | None = None
    url: str | None = Field(default=None, min_length=1)
    secret_header: str | None = Field(default=None, min_length=1)
    secret_value: str | None = Field(default=None, min_length=1)


class ToolOut(BaseModel):
    """A tool as the settings page shows it — its configuration, but never a secret's value."""

    id: uuid.UUID
    name: str
    description: str
    parameters: dict[str, Any]
    kind: ToolKind
    enabled: bool
    method: str | None
    url: str | None
    secret_header: str | None
    has_secret: bool
    mcp_server_id: uuid.UUID | None
    created_at: datetime


class ToolImageOut(BaseModel):
    """An image a tool call returned — fetched from the attachment content route by its id."""

    id: uuid.UUID
    mime: str


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
    images: list[ToolImageOut] = []
    created_at: datetime
