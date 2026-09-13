"""One document within a connector — a crawled page (for a WEBSITE connector) or an uploaded file
(for a FILE connector). Its extracted text is chunked and embedded, never stored on this row."""

import uuid

from sqlalchemy import Enum, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.document_status import DocumentStatus
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class ConnectorDocument(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "connector_documents"

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    connector_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("connectors.id", ondelete="CASCADE"))
    # Set for a crawled page; None for an uploaded file.
    source_url: Mapped[str | None] = mapped_column(default=None)
    # Set for an uploaded file; None for a crawled page.
    filename: Mapped[str | None] = mapped_column(default=None)
    mime: Mapped[str]
    size: Mapped[int] = mapped_column(Integer, default=0)
    # None until a file's bytes are written to disk (a page fetched fresh each index has none).
    storage_key: Mapped[str | None] = mapped_column(default=None)
    title: Mapped[str | None] = mapped_column(default=None)
    char_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    status: Mapped[DocumentStatus] = mapped_column(
        Enum(DocumentStatus, name="document_status"), default=DocumentStatus.PENDING
    )
    error: Mapped[str | None] = mapped_column(default=None)
