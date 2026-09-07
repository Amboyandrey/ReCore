"""One provider model, enabled for chat in a workspace via one of its credentials."""

import uuid

from sqlalchemy import ForeignKey, Numeric, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class LLMModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A model made available for chat — one credential can back several enabled models."""

    __tablename__ = "models"
    __table_args__ = (UniqueConstraint("credential_id", "provider_model_id"),)

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    credential_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("provider_credentials.id", ondelete="CASCADE")
    )
    provider_model_id: Mapped[str]
    display_name: Mapped[str]
    context_window: Mapped[int | None] = mapped_column(default=None)
    cost_per_mtok_in: Mapped[float | None] = mapped_column(Numeric(10, 4, asdecimal=False), default=None)
    cost_per_mtok_out: Mapped[float | None] = mapped_column(Numeric(10, 4, asdecimal=False), default=None)
    enabled: Mapped[bool] = mapped_column(default=True, server_default="true")
