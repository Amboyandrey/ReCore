"""A remote MCP server a workspace connects to — its tools are discovered from it and stored as
ordinary `tools` rows of kind MCP, so the agent loop never needs to know where they came from."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, LargeBinary, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class McpServer(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One Streamable HTTP MCP endpoint, scoped to a workspace.

    `name` prefixes every tool discovered from it (`<name>__<tool>`), so it's unique per workspace
    and held to the same character rule a tool name is. `auth_header` plus the envelope-encrypted
    value is sent on every request, the same optional-secret shape an HTTP tool uses; each of its
    tool rows carries a copy of the URL and secret so executing one never needs this row loaded.
    """

    __tablename__ = "mcp_servers"
    __table_args__ = (UniqueConstraint("workspace_id", "name", name="uq_mcp_servers_workspace_name"),)

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    name: Mapped[str]
    url: Mapped[str]
    auth_header: Mapped[str | None] = mapped_column(default=None)
    ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary, default=None)
    nonce: Mapped[bytes | None] = mapped_column(LargeBinary, default=None)
    wrapped_key: Mapped[bytes | None] = mapped_column(LargeBinary, default=None)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
