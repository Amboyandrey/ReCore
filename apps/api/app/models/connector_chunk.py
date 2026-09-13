"""One embedded chunk of a connector document's text — what retrieval actually searches over.

`embedding` has no fixed dimension at the column level: a workspace can change its embedding
model, which changes the dimension, and different connectors may be re-indexed at different times.
Retrieval only ever compares chunks against a query embedded by the *same* model (enforced in
services/knowledge.py by only searching connectors whose `embedding_model_id` still matches the
workspace's current setting), so a mixed-dimension table is safe — the column just doesn't get to
declare one dimension a Postgres CHECK could enforce.
"""

import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class ConnectorChunk(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "connector_chunks"

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    connector_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("connectors.id", ondelete="CASCADE"))
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("connector_documents.id", ondelete="CASCADE"))
    ordinal: Mapped[int] = mapped_column(Integer)
    content: Mapped[str]
    embedding: Mapped[list[float]] = mapped_column(Vector())
