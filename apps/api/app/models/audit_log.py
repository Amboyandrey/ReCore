"""An immutable record of a privileged action — who did what, to what, from where."""

import uuid
from typing import Any

from sqlalchemy import JSON, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class AuditLog(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One privileged action. Never updated or deleted — only ever appended to.

    `workspace_id` is null for actions that aren't tenant-scoped (platform-wide flag admin, for
    instance); every workspace-scoped action carries it, so a workspace's own audit view is a
    plain filter, not a join.
    """

    __tablename__ = "audit_logs"

    actor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), default=None
    )
    action: Mapped[str]
    target_type: Mapped[str]
    target_id: Mapped[str]
    ip: Mapped[str]
    # Named "metadata" at the database level (per ARCHITECTURE.md's data model) but exposed here
    # as `event_metadata` — `metadata` is a reserved attribute name on every declarative model.
    event_metadata: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSON, default=None)
