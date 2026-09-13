"""A workspace's own choice of embedding model — one per workspace, chosen by an admin, so every
connector's chunks are comparable vectors and retrieval is a single query."""

import uuid

from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.mixins import TimestampMixin


class KnowledgeSettings(Base, TimestampMixin):
    """Exactly one per workspace — `workspace_id` is this table's own primary key rather than a
    separate surrogate one, same shape `MemoryCredential` uses for the mem0 key."""

    __tablename__ = "knowledge_settings"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    embedding_model_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("models.id", ondelete="SET NULL"), default=None
    )
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
