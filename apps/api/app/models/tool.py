"""A function the model can call mid-reply — the built-in web search, a workspace's own
third-party HTTP tool, or one discovered from a connected MCP server."""

import uuid
from typing import Any

from sqlalchemy import JSON, Enum, ForeignKey, LargeBinary, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.models.tool_kind import ToolKind


class Tool(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One tool offered to the model, scoped to a workspace.

    `name` is the identifier the model itself sees and calls by — unique per workspace so the
    agent loop never has to guess which of two same-named tools a call meant. `parameters` is the
    JSON Schema object passed straight through to whichever provider adapter is in use.

    `ciphertext`/`nonce`/`wrapped_key` hold an envelope-encrypted secret exactly like a provider
    credential's (see app/core/crypto.py) — the built-in web search's own Tavily key for a
    `BUILTIN` row, or an HTTP tool's optional secret header value for an `HTTP` one. `method`,
    `url`, and `secret_header` stay null on a `BUILTIN` row; there's nowhere else to send the
    request.

    An `MCP` row belongs to `mcp_server_id` and is called on it as `remote_name` — `name` is the
    server-prefixed one the model sees. Its `url`, `secret_header`, and secret are copies of the
    server's, kept in step by services/mcp_servers.py, so executing it needs no extra lookup.
    """

    __tablename__ = "tools"
    __table_args__ = (UniqueConstraint("workspace_id", "name", name="uq_tools_workspace_name"),)

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    name: Mapped[str]
    description: Mapped[str]
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON)
    kind: Mapped[ToolKind] = mapped_column(Enum(ToolKind, name="tool_kind"))
    enabled: Mapped[bool] = mapped_column(default=True)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))

    # HTTP-tool-only — see class docstring.
    method: Mapped[str | None] = mapped_column(default=None)
    url: Mapped[str | None] = mapped_column(default=None)
    secret_header: Mapped[str | None] = mapped_column(default=None)

    # MCP-tool-only — see class docstring.
    mcp_server_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("mcp_servers.id", ondelete="CASCADE"), default=None
    )
    remote_name: Mapped[str | None] = mapped_column(default=None)

    # An optional encrypted secret — the Tavily key for BUILTIN web search, or an HTTP tool's
    # secret header value. Null when a tool has no secret to hold.
    ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary, default=None)
    nonce: Mapped[bytes | None] = mapped_column(LargeBinary, default=None)
    wrapped_key: Mapped[bytes | None] = mapped_column(LargeBinary, default=None)
