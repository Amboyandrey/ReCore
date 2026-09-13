"""A workspace's own knowledge connectors — a website crawl or a set of uploaded files, indexed
in the background (see app/workers/index_connector.py) so an assistant can retrieve from them.

Embedding is a workspace-wide choice (one model, set once in Knowledge settings): every connector
is indexed with it, so every chunk's vector shares one dimension and retrieval is a single query
rather than one per embedding model in use. A connector snapshots the model (and dimension) it was
actually indexed with — not the workspace's live setting — so changing the setting later shows up
as that connector needing a re-index instead of silently mixing incompatible vectors.
"""

import uuid
from dataclasses import dataclass
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
    AssistantConnector,
    Connector,
    ConnectorChunk,
    ConnectorDocument,
    ConnectorKind,
    ConnectorStatus,
    KnowledgeSettings,
    LLMModel,
    Message,
    MessageSource,
    ModelKind,
)
from app.providers.base import EmbeddingProvider
from app.providers.registry import build_embedding_provider, supports_embeddings
from app.services.credentials import decrypt_credential_key, get_credential

settings = get_settings()

# A connector indexes on the workspace's own credentials and a shared worker — unlike most
# per-workspace resources in this codebase, that needs a hard ceiling, not just a UI suggestion.
MAX_CONNECTORS_PER_WORKSPACE = 20

# How many distinct sources one turn folds in, how close (cosine distance, lower = closer) a
# chunk must be to even qualify, and the character budget for the whole block — same reasoning
# memory's own _MEMORY_BLOCK_MAX_CHARS follows: knowledge shouldn't crowd out the conversation.
_RETRIEVAL_TOP_K = 8
_RETRIEVAL_MAX_DISTANCE = 0.55
_RETRIEVAL_MAX_CHARS = 6_000


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


@dataclass(frozen=True)
class SourceHit:
    """One chunk that was actually folded into a reply's prompt — what the chat UI's sources
    sidebar shows for that message (see app/services/chat.py's fold-in and the `message_sources`
    table those get persisted to)."""

    ordinal: int
    connector_id: uuid.UUID
    connector_name: str
    document_id: uuid.UUID
    label: str
    url: str | None
    snippet: str
    score: float


@dataclass(frozen=True)
class KnowledgeResult:
    block: str
    sources: list[SourceHit]


async def retrieve_for_turn(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    assistant_id: uuid.UUID,
    query: str,
    top_k: int = _RETRIEVAL_TOP_K,
    max_distance: float = _RETRIEVAL_MAX_DISTANCE,
    max_chars: int = _RETRIEVAL_MAX_CHARS,
) -> KnowledgeResult | None:
    """Search the assistant's own attached, ready, current-embedding-model connectors for chunks
    relevant to `query`, and return them as a labeled block to fold into the system prompt,
    alongside the per-chunk sources the sidebar shows.

    Never raises and never fails a turn: a missing embedding setting, no matching connectors, or
    any embedding/query failure all just mean no knowledge reaches this turn — the same
    "degrade silently" contract services/memory.py's own retrieve_for_turn follows.

    Only connectors whose `embedding_model_id` still matches the workspace's *current* setting
    are searched — a connector indexed under a since-changed model has a vector space that isn't
    comparable to a freshly embedded query, so it's skipped (and shown as "needs reindex" in the
    UI) rather than silently returning nonsense distances.
    """
    try:
        settings_row = await get_settings_row(db, workspace_id=workspace_id)
        if settings_row is None or settings_row.embedding_model_id is None:
            return None

        ready_ids = list(
            (
                await db.scalars(
                    select(Connector.id)
                    .join(AssistantConnector, AssistantConnector.connector_id == Connector.id)
                    .where(
                        AssistantConnector.assistant_id == assistant_id,
                        Connector.status == ConnectorStatus.READY,
                        Connector.embedding_model_id == settings_row.embedding_model_id,
                    )
                )
            ).all()
        )
        if not ready_ids:
            return None

        vectors, _model = await embed_texts(db, workspace_id=workspace_id, texts=[query])
        distance = ConnectorChunk.embedding.cosine_distance(vectors[0])
        stmt = (
            select(ConnectorChunk, ConnectorDocument, Connector.name, distance.label("distance"))
            .join(ConnectorDocument, ConnectorChunk.document_id == ConnectorDocument.id)
            .join(Connector, ConnectorChunk.connector_id == Connector.id)
            .where(ConnectorChunk.connector_id.in_(ready_ids), distance <= max_distance)
            .order_by(distance)
            .limit(top_k * 2)
        )
        rows = (await db.execute(stmt)).all()
    except Exception:  # noqa: BLE001 — a retrieval failure must never fail the turn itself
        return None

    if not rows:
        return None

    sources: list[SourceHit] = []
    seen_documents: set[uuid.UUID] = set()
    lines: list[str] = []
    total_chars = 0
    for chunk, document, connector_name, dist in rows:
        if len(sources) >= top_k:
            break
        if document.id in seen_documents:
            continue
        title = document.title or document.filename or document.source_url or connector_name
        label = f"{title} — {document.source_url}" if document.source_url else title
        ordinal = len(sources) + 1
        line = f"[Source {ordinal}: {label}]\n{chunk.content}"
        if total_chars + len(line) > max_chars:
            break
        seen_documents.add(document.id)
        lines.append(line)
        total_chars += len(line)
        sources.append(
            SourceHit(
                ordinal=ordinal,
                connector_id=chunk.connector_id,
                connector_name=connector_name,
                document_id=document.id,
                label=title,
                url=document.source_url,
                snippet=chunk.content[:300],
                score=1 - float(dist),
            )
        )

    if not sources:
        return None
    return KnowledgeResult(block="Relevant knowledge:\n" + "\n\n".join(lines), sources=sources)


async def list_message_sources(db: AsyncSession, *, conversation_id: uuid.UUID) -> list[MessageSource]:
    """List every knowledge source folded into any reply in a conversation, oldest first.

    Joined through messages since a MessageSource only carries the message_id it was recorded
    for, not a conversation_id of its own — same reach-through-the-parent shape
    services/tools.py's list_tool_invocations already uses for ToolInvocation.
    """
    stmt = (
        select(MessageSource)
        .join(Message, Message.id == MessageSource.message_id)
        .where(Message.conversation_id == conversation_id)
        .order_by(MessageSource.created_at, MessageSource.ordinal)
    )
    return list((await db.scalars(stmt)).all())
