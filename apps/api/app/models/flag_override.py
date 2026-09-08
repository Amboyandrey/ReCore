"""A targeting rule that beats a flag's default for one user or one workspace."""

import uuid
from typing import Any

from sqlalchemy import JSON, Enum, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.flag_scope import FlagScope
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class FlagOverride(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Pins one flag to a value for one user or workspace, ahead of any rollout or default."""

    __tablename__ = "flag_overrides"
    __table_args__ = (UniqueConstraint("flag_id", "scope", "scope_id"),)

    flag_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("feature_flags.id", ondelete="CASCADE"))
    scope: Mapped[FlagScope] = mapped_column(Enum(FlagScope, name="flag_scope"))
    # A user id or a workspace id depending on `scope` — polymorphic, so no FK: either table
    # could be the target and only one of them applies per row.
    scope_id: Mapped[uuid.UUID]
    value: Mapped[Any] = mapped_column(JSON)
