"""A flag definition — the default every workspace gets before any override or rollout applies."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Enum, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.flag_type import FlagType
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class FeatureFlag(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One flag, global across the platform — workspaces and users only ever override its value."""

    __tablename__ = "feature_flags"

    key: Mapped[str] = mapped_column(unique=True, index=True)
    description: Mapped[str]
    type: Mapped[FlagType] = mapped_column(Enum(FlagType, name="flag_type"))
    default_value: Mapped[Any] = mapped_column(JSON)
    # Deterministic hash(key + ":" + workspace_id) % 100 < this enables the flag before falling
    # back to default_value — null means no rollout tier, go straight to the default.
    rollout_percentage: Mapped[int | None] = mapped_column(Integer, default=None)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
