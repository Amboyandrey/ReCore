"""The tenant boundary: a workspace, and who belongs to it at what role."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.models.role import Role


class Workspace(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A tenant — every workspace-scoped row elsewhere points back to one of these."""

    __tablename__ = "workspaces"

    slug: Mapped[str] = mapped_column(unique=True, index=True)
    name: Mapped[str]
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    settings: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict, server_default="{}")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class WorkspaceMember(Base):
    """One user's membership in one workspace — the composite key a user can only hold once."""

    __tablename__ = "workspace_members"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    role: Mapped[Role] = mapped_column(Enum(Role, name="role"))
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
