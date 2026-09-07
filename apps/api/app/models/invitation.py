"""A pending seat in a workspace — the token is stored hashed, and lives for a limited time."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.models.role import Role


class Invitation(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """An invite to join a workspace at a given role, redeemable once before it expires."""

    __tablename__ = "invitations"

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    email: Mapped[str] = mapped_column(CITEXT)
    role: Mapped[Role] = mapped_column(Enum(Role, name="role"))
    token_hash: Mapped[str] = mapped_column(unique=True, index=True)
    invited_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
