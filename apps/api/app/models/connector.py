"""A workspace's own source of knowledge for assistants to retrieve from — a website to crawl, or
a set of uploaded files. Indexed in the background (see app/workers/index_connector.py)."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.connector_kind import ConnectorKind
from app.models.connector_status import ConnectorStatus
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class Connector(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One connector. `embedding_model_id`/`embedding_dim` are a snapshot of what it was actually
    indexed with — not the workspace's current setting — so a later change to that setting shows
    up as "needs re-index" instead of silently mixing incompatible vector dimensions at retrieval.
    """

    __tablename__ = "connectors"

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    kind: Mapped[ConnectorKind] = mapped_column(Enum(ConnectorKind, name="connector_kind"))
    name: Mapped[str]
    # Set only for a WEBSITE connector — the crawl's starting point.
    url: Mapped[str | None] = mapped_column(default=None)
    max_pages: Mapped[int] = mapped_column(Integer, default=30, server_default="30")
    status: Mapped[ConnectorStatus] = mapped_column(
        Enum(ConnectorStatus, name="connector_status"), default=ConnectorStatus.PENDING
    )
    error: Mapped[str | None] = mapped_column(default=None)
    embedding_model_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("models.id", ondelete="SET NULL"), default=None
    )
    embedding_dim: Mapped[int | None] = mapped_column(default=None)
    document_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
