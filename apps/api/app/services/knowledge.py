"""A workspace's own knowledge connectors — a website crawl or a set of uploaded files, indexed
in the background (see app/workers/index_connector.py) so an assistant can retrieve from them.

Embedding is a workspace-wide choice (one model, set once in Knowledge settings): every connector
is indexed with it, so every chunk's vector shares one dimension and retrieval is a single query
rather than one per embedding model in use. A connector snapshots the model (and dimension) it was
actually indexed with — not the workspace's live setting — so changing the setting later shows up
as that connector needing a re-index instead of silently mixing incompatible vectors.
"""

import uuid
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import (
    AttachmentTooLarge,
    ConnectorLimitReached,
    ConnectorNotFound,
    EmbeddingsNotSupported,
    KnowledgeNotConfigured,
    ModelNotFound,
)
from app.core.ssrf import assert_safe_base_url
from app.models import (
    Connector,
    ConnectorDocument,
    ConnectorKind,
    ConnectorStatus,
    KnowledgeSettings,
    LLMModel,
    ModelKind,
)
from app.providers.base import EmbeddingProvider
from app.providers.registry import build_embedding_provider, supports_embeddings
from app.services.credentials import decrypt_credential_key, get_credential

settings = get_settings()

# A connector indexes on the workspace's own credentials and a shared worker — unlike most
# per-workspace resources in this codebase, that needs a hard ceiling, not just a UI suggestion.
MAX_CONNECTORS_PER_WORKSPACE = 20


async def get_settings_row(db: AsyncSession, *, workspace_id: uuid.UUID) -> KnowledgeSettings | None:
    """The workspace's chosen embedding model, if one's been set."""
    return await db.get(KnowledgeSettings, workspace_id)


async def set_embedding_model(
    db: AsyncSession, *, workspace_id: uuid.UUID, model_id: uuid.UUID | None, user_id: uuid.UUID
) -> KnowledgeSettings:
    """Choose (or clear, passing `None`) the workspace's embedding model. The model must belong
    to this workspace, be marked `kind=EMBEDDING`, and its provider must actually support
    embeddings — Anthropic-backed models are never eligible, regardless of how they're marked."""
    if model_id is not None:
        model = await db.scalar(
            select(LLMModel).where(LLMModel.id == model_id, LLMModel.workspace_id == workspace_id)
        )
        if model is None:
            raise ModelNotFound()
        if model.kind != ModelKind.EMBEDDING:
            raise EmbeddingsNotSupported()
        credential = await get_credential(db, workspace_id=workspace_id, credential_id=model.credential_id)
        if not supports_embeddings(credential.provider):
            raise EmbeddingsNotSupported()

    row = await get_settings_row(db, workspace_id=workspace_id)
    if row is None:
        row = KnowledgeSettings(workspace_id=workspace_id, embedding_model_id=model_id, created_by=user_id)
        db.add(row)
    else:
        row.embedding_model_id = model_id
    await db.flush()
    return row


async def embed_texts(
    db: AsyncSession, *, workspace_id: uuid.UUID, texts: list[str]
) -> tuple[list[list[float]], LLMModel]:
    """Embed `texts` with the workspace's chosen model, returning the vectors alongside the model
    used (callers need its id and dimension to stamp onto what they're indexing)."""
    row = await get_settings_row(db, workspace_id=workspace_id)
    if row is None or row.embedding_model_id is None:
        raise KnowledgeNotConfigured()
    model = await db.get(LLMModel, row.embedding_model_id)
    if model is None or not model.enabled:
        raise KnowledgeNotConfigured()
    credential = await get_credential(db, workspace_id=workspace_id, credential_id=model.credential_id)
    adapter: EmbeddingProvider = build_embedding_provider(
        credential.provider, api_key=decrypt_credential_key(credential), base_url=credential.base_url
    )
    vectors = await adapter.embed(model=model.provider_model_id, texts=texts)
    return vectors, model


async def _assert_connector_capacity(db: AsyncSession, *, workspace_id: uuid.UUID) -> None:
    total = await db.scalar(
        select(func.count()).select_from(Connector).where(Connector.workspace_id == workspace_id)
    )
    if (total or 0) >= MAX_CONNECTORS_PER_WORKSPACE:
        raise ConnectorLimitReached()


async def create_website_connector(
    db: AsyncSession, *, workspace_id: uuid.UUID, user_id: uuid.UUID, name: str, url: str, max_pages: int = 30
) -> Connector:
    """Register a website connector — indexing itself is queued by the caller (the router) once
    this row exists, via services/jobs.py."""
    await _assert_connector_capacity(db, workspace_id=workspace_id)
    assert_safe_base_url(url)
    connector = Connector(
        workspace_id=workspace_id,
        kind=ConnectorKind.WEBSITE,
        name=name,
        url=url,
        max_pages=max(1, min(max_pages, 100)),
        status=ConnectorStatus.PENDING,
        created_by=user_id,
    )
    db.add(connector)
    await db.flush()
    return connector


async def create_file_connector(
    db: AsyncSession, *, workspace_id: uuid.UUID, user_id: uuid.UUID, name: str
) -> Connector:
    """Register a file connector, with no documents yet — add_document attaches them."""
    await _assert_connector_capacity(db, workspace_id=workspace_id)
    connector = Connector(
        workspace_id=workspace_id,
        kind=ConnectorKind.FILE,
        name=name,
        status=ConnectorStatus.PENDING,
        created_by=user_id,
    )
    db.add(connector)
    await db.flush()
    return connector


async def add_document(
    db: AsyncSession, *, connector: Connector, filename: str, mime: str, data: bytes
) -> ConnectorDocument:
    """Write an uploaded file to disk and record it — the worker reads it back at index time
    (see app/workers/index_connector.py), same storage layout as chat attachments but under this
    connector rather than a conversation."""
    if len(data) > settings.max_attachment_size_bytes:
        raise AttachmentTooLarge()
    document_id = uuid.uuid4()
    storage_key = f"{connector.workspace_id}/connectors/{document_id}"
    path = Path(settings.storage_dir) / storage_key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)

    document = ConnectorDocument(
        id=document_id,
        workspace_id=connector.workspace_id,
        connector_id=connector.id,
        filename=filename,
        mime=mime,
        size=len(data),
        storage_key=storage_key,
    )
    db.add(document)
    await db.flush()
    return document


async def delete_document(
    db: AsyncSession, *, workspace_id: uuid.UUID, connector_id: uuid.UUID, document_id: uuid.UUID
) -> None:
    """Remove one document from a file connector, and its bytes on disk — its chunks (if it was
    ever indexed) cascade with it."""
    document = await db.scalar(
        select(ConnectorDocument).where(
            ConnectorDocument.id == document_id,
            ConnectorDocument.connector_id == connector_id,
            ConnectorDocument.workspace_id == workspace_id,
        )
    )
    if document is None:
        raise ConnectorNotFound()
    if document.storage_key is not None:
        (Path(settings.storage_dir) / document.storage_key).unlink(missing_ok=True)
    await db.delete(document)
    await db.flush()


async def get_connector(db: AsyncSession, *, workspace_id: uuid.UUID, connector_id: uuid.UUID) -> Connector:
    connector = await db.scalar(
        select(Connector).where(Connector.id == connector_id, Connector.workspace_id == workspace_id)
    )
    if connector is None:
        raise ConnectorNotFound()
    return connector


async def list_connectors(db: AsyncSession, *, workspace_id: uuid.UUID) -> list[Connector]:
    stmt = select(Connector).where(Connector.workspace_id == workspace_id).order_by(Connector.created_at)
    return list((await db.scalars(stmt)).all())


async def list_documents(db: AsyncSession, *, connector_id: uuid.UUID) -> list[ConnectorDocument]:
    stmt = (
        select(ConnectorDocument)
        .where(ConnectorDocument.connector_id == connector_id)
        .order_by(ConnectorDocument.created_at)
    )
    return list((await db.scalars(stmt)).all())


async def reindex_connector(
    db: AsyncSession, *, workspace_id: uuid.UUID, connector_id: uuid.UUID
) -> Connector:
    """Mark a connector for re-indexing — the caller (the router) enqueues the job once this
    commits. Clears any previous error; the old chunks stay searchable until the new index
    actually replaces them (the worker deletes them at the start of the run, not here)."""
    connector = await get_connector(db, workspace_id=workspace_id, connector_id=connector_id)
    connector.status = ConnectorStatus.PENDING
    connector.error = None
    await db.flush()
    return connector


async def delete_connector(db: AsyncSession, *, workspace_id: uuid.UUID, connector_id: uuid.UUID) -> None:
    """Permanently remove a connector — its documents and chunks cascade; any files it owns on
    disk are removed too. An assistant that had it attached just loses that source, the same
    fallback a deleted tool or delegate already gets."""
    connector = await get_connector(db, workspace_id=workspace_id, connector_id=connector_id)
    for document in await list_documents(db, connector_id=connector.id):
        if document.storage_key is not None:
            (Path(settings.storage_dir) / document.storage_key).unlink(missing_ok=True)
    await db.execute(delete(Connector).where(Connector.id == connector.id))
    await db.flush()


def needs_reindex(connector: Connector, settings_row: KnowledgeSettings | None) -> bool:
    """Whether the workspace's current embedding model differs from what this connector was last
    indexed with — including a connector that's never been indexed at all (embedding_model_id is
    still None), and a workspace with no embedding model chosen (nothing could ever be current)."""
    if settings_row is None or settings_row.embedding_model_id is None:
        return True
    return connector.embedding_model_id != settings_row.embedding_model_id
