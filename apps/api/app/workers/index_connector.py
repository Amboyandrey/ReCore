"""The arq job that actually indexes one connector — crawls a website or reads a file connector's
uploaded documents, chunks and embeds the text, and stores the result as searchable chunks.

Runs in the worker process (see app/workers/main.py), entirely independent of any HTTP request:
it opens its own database session and re-establishes row-level-security scope after every commit
(that scope is transaction-local — the same reasoning app/services/chat.py's own background
generation task follows). Any failure anywhere in the run — a bad URL, a corrupt file, a misused
embedding credential — lands the connector in FAILED with the error recorded, never as an
unhandled exception that would just look like a job silently vanishing.
"""

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select

from app.core.config import get_settings
from app.core.db import async_session_factory, set_workspace_scope
from app.knowledge.chunk import chunk_text
from app.knowledge.crawl import crawl
from app.models import (
    Connector,
    ConnectorChunk,
    ConnectorDocument,
    ConnectorKind,
    ConnectorStatus,
    DocumentStatus,
    ExtractStatus,
)
from app.services.attachments import extract_text
from app.services.knowledge import embed_texts

settings = get_settings()

# Bounds how much of a connector's own worth of vectors accumulates — a runaway crawl or a huge
# batch of files shouldn't be free to grow one connector's index without limit.
MAX_CHUNKS_PER_CONNECTOR = 2000


async def _crawl_website(connector: Connector) -> list[tuple[str, str, str]]:
    """Crawl a WEBSITE connector's start URL, returning `(source_url, title, text)` per page —
    never touches the database; the caller turns each into a fresh ConnectorDocument."""
    assert connector.url is not None  # enforced at creation — a WEBSITE connector always has one
    pages = await crawl(connector.url, max_pages=connector.max_pages)
    return [(page.url, page.title, page.text) for page in pages]


def _read_file_document(document: ConnectorDocument) -> str:
    """Read one FILE connector document's bytes back off disk and extract its text, uncapped —
    unlike a chat attachment, indexing chunks the full text itself rather than needing it
    pre-truncated to a single message's context budget."""
    assert document.storage_key is not None  # every FILE document has one, set at upload time
    data = (Path(settings.storage_dir) / document.storage_key).read_bytes()
    text, extract_status, error = extract_text(data, document.mime, max_chars=None)
    if extract_status != ExtractStatus.DONE or text is None:
        raise ValueError(error or "No extractable text found.")
    return text


async def index_connector(ctx: dict[str, Any], connector_id: str, workspace_id: str) -> None:
    """Index one connector end to end. `ctx` is arq's own per-job context, unused here."""
    del ctx
    cid, wid = uuid.UUID(connector_id), uuid.UUID(workspace_id)

    async with async_session_factory() as db:
        await set_workspace_scope(db, wid)
        connector = await db.get(Connector, cid)
        if connector is None:
            return  # deleted before the job ran — nothing to do
        connector.status = ConnectorStatus.INDEXING
        connector.error = None
        await db.commit()

        try:
            await set_workspace_scope(db, wid)  # transaction-local — reset after every commit
            connector = await db.get(Connector, cid)
            assert connector is not None

            documents: list[ConnectorDocument]
            texts: dict[uuid.UUID, str] = {}

            if connector.kind == ConnectorKind.WEBSITE:
                # A website connector's documents are entirely a byproduct of the last crawl —
                # replaced wholesale each time, unlike a file connector's, which the user uploaded
                # and expects to persist across a reindex.
                await db.execute(delete(ConnectorDocument).where(ConnectorDocument.connector_id == cid))
                pages = await _crawl_website(connector)
                documents = []
                for source_url, title, text in pages:
                    document = ConnectorDocument(
                        workspace_id=wid,
                        connector_id=cid,
                        source_url=source_url,
                        mime="text/html",
                        title=title or None,
                        char_count=len(text),
                        status=DocumentStatus.PENDING,
                    )
                    db.add(document)
                    documents.append(document)
                await db.flush()
                for document, (_, _, text) in zip(documents, pages, strict=True):
                    texts[document.id] = text
            else:
                await db.execute(delete(ConnectorChunk).where(ConnectorChunk.connector_id == cid))
                documents = list(
                    (
                        await db.scalars(
                            select(ConnectorDocument).where(ConnectorDocument.connector_id == cid)
                        )
                    ).all()
                )

            total_chunks = 0
            embedding_model_id: uuid.UUID | None = None
            embedding_dim: int | None = None

            for document in documents:
                if total_chunks >= MAX_CHUNKS_PER_CONNECTOR:
                    document.status = DocumentStatus.FAILED
                    document.error = "Connector-wide chunk limit reached before this document."
                    continue
                try:
                    if connector.kind == ConnectorKind.WEBSITE:
                        text = texts[document.id]
                    else:
                        text = _read_file_document(document)
                        document.char_count = len(text)
                except Exception as exc:  # noqa: BLE001 — one bad document must not fail the run
                    document.status = DocumentStatus.FAILED
                    document.error = str(exc)[:1000]
                    continue

                pieces = chunk_text(text)[: MAX_CHUNKS_PER_CONNECTOR - total_chunks]
                if not pieces:
                    document.status = DocumentStatus.FAILED
                    document.error = "No extractable text found."
                    continue

                vectors, model = await embed_texts(db, workspace_id=wid, texts=pieces)
                embedding_model_id, embedding_dim = model.id, len(vectors[0])
                for ordinal, (piece, vector) in enumerate(zip(pieces, vectors, strict=True)):
                    db.add(
                        ConnectorChunk(
                            workspace_id=wid,
                            connector_id=cid,
                            document_id=document.id,
                            ordinal=ordinal,
                            content=piece,
                            embedding=vector,
                        )
                    )
                total_chunks += len(pieces)
                document.status = DocumentStatus.DONE

            connector.status = ConnectorStatus.READY
            connector.embedding_model_id = embedding_model_id
            connector.embedding_dim = embedding_dim
            connector.document_count = len(documents)
            connector.chunk_count = total_chunks
            connector.last_indexed_at = datetime.now(UTC)
            await db.commit()
        except Exception as exc:  # noqa: BLE001 — a connector must land in FAILED, never crash silently
            await db.rollback()
            await set_workspace_scope(db, wid)
            failed = await db.get(Connector, cid)
            if failed is not None:
                failed.status = ConnectorStatus.FAILED
                failed.error = str(exc)[:1000]
                await db.commit()
