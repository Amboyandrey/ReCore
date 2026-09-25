"""Request and response shapes for a workspace's MCP servers. The auth value a server is reached
with never appears in any response — only in the request that sets it."""

import uuid
from datetime import datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

# The tool-name rule (see schemas/tool.py), capped at 32 so `<server>__<tool>` has room left.
_SERVER_NAME_PATTERN = r"^[a-zA-Z0-9_-]+$"


class McpServerCreate(BaseModel):
    """Connecting a server: a short name that prefixes its tools, its Streamable HTTP endpoint,
    and an optional header (e.g. `Authorization`) plus value sent on every request."""

    # A pasted URL's stray leading space passes the SSRF guard's parse but not the MCP client's.
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=32, pattern=_SERVER_NAME_PATTERN)
    url: str = Field(min_length=1)
    auth_header: str | None = Field(default=None, min_length=1)
    auth_value: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _header_and_value_together(self) -> Self:
        """An auth header with no value (or the reverse) can never authenticate anything."""
        if (self.auth_header is None) != (self.auth_value is None):
            raise ValueError("auth_header and auth_value must be given together.")
        return self


class McpServerUpdate(BaseModel):
    """Changing how a server is reached — only the fields actually sent are touched. Sending
    `auth_value` rotates the stored secret; omitting it leaves the current one in place."""

    model_config = ConfigDict(str_strip_whitespace=True)

    url: str | None = Field(default=None, min_length=1)
    auth_header: str | None = Field(default=None, min_length=1)
    auth_value: str | None = Field(default=None, min_length=1)


class McpServerOut(BaseModel):
    """A server as the settings page shows it — its tools are listed with the workspace's other
    tools, carrying this server's id."""

    id: uuid.UUID
    name: str
    url: str
    auth_header: str | None
    has_secret: bool
    last_synced_at: datetime | None
    created_at: datetime
