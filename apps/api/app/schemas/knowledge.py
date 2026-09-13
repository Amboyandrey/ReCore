"""Request and response shapes for a workspace's knowledge settings and connectors."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models import ConnectorKind, ConnectorStatus, DocumentStatus
from app.schemas.model import ModelOut


class KnowledgeSettingsUpdate(BaseModel):
    """Choose the workspace's embedding model — every connector is indexed with it. `null`
    clears it (existing connectors keep their own indexed vectors, but nothing more can be
    indexed or retrieved until a model is chosen again)."""

    embedding_model_id: uuid.UUID | None = None


class KnowledgeSettingsOut(BaseModel):
    embedding_model_id: uuid.UUID | None
    embedding_model: ModelOut | None


class WebsiteConnectorCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    url: str = Field(min_length=1)
    max_pages: int = Field(default=30, ge=1, le=100)


class ConnectorDocumentOut(BaseModel):
    id: uuid.UUID
    source_url: str | None
    filename: str | None
    mime: str
    size: int
    title: str | None
    char_count: int
    status: DocumentStatus
    error: str | None
    created_at: datetime


class ConnectorOut(BaseModel):
    """One connector. `needs_reindex` is true when the workspace's current embedding model
    differs from the one this connector was actually indexed with (or it's never been indexed) —
    those connectors are skipped at retrieval rather than mixing incompatible vector dimensions.
    """

    id: uuid.UUID
    kind: ConnectorKind
    name: str
    url: str | None
    max_pages: int
    status: ConnectorStatus
    error: str | None
    embedding_model_id: uuid.UUID | None
    embedding_dim: int | None
    document_count: int
    chunk_count: int
    last_indexed_at: datetime | None
    needs_reindex: bool
    created_by: uuid.UUID
    created_at: datetime


class ConnectorDetailOut(ConnectorOut):
    documents: list[ConnectorDocumentOut]
