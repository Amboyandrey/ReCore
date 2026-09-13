"""The arq job that actually indexes a connector — website crawl or file read, chunk, embed,
store — against a real (committing) database session, the same `db` fixture chat's own
background-generation tests use, since index_connector opens its own session independent of any
fixture's transaction."""

import uuid
from pathlib import Path

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import encrypt_secret
from app.knowledge import crawl as crawl_module
from app.models import (
    Connector,
    ConnectorChunk,
    ConnectorDocument,
    ConnectorKind,
    ConnectorStatus,
    DocumentStatus,
    KnowledgeSettings,
    LLMModel,
    ModelKind,
    Provider,
    ProviderCredential,
    User,
    Workspace,
)
from app.providers.fake import VALID_KEY, FakeProvider
from app.workers.index_connector import index_connector


async def _workspace_with_embedding_model(db: AsyncSession) -> tuple[User, Workspace, LLMModel]:
    """A user, workspace, an embedding-kind model, and the workspace's own choice of it — the
    minimum index_connector needs to actually embed anything."""
    user = User(email=f"idx-{uuid.uuid4().hex[:8]}@example.com", password_hash="hashed")
    db.add(user)
    await db.flush()
    workspace = Workspace(slug=f"idx-{uuid.uuid4().hex[:8]}", name="Idx", owner_id=user.id)
    db.add(workspace)
    await db.flush()
    secret = encrypt_secret(VALID_KEY)
    credential = ProviderCredential(
        workspace_id=workspace.id,
        provider=Provider.OPENAI,
        label="Embeddings",
        ciphertext=secret.ciphertext,
        nonce=secret.nonce,
        wrapped_key=secret.wrapped_key,
        last4=VALID_KEY[-4:],
        created_by=user.id,
    )
    db.add(credential)
    await db.flush()
    model = LLMModel(
        workspace_id=workspace.id,
        credential_id=credential.id,
        provider_model_id="fake-embed",
        display_name="Fake Embed",
        kind=ModelKind.EMBEDDING,
    )
    db.add(model)
    await db.flush()
    db.add(KnowledgeSettings(workspace_id=workspace.id, embedding_model_id=model.id, created_by=user.id))
    await db.commit()
    return user, workspace, model


@pytest.fixture(autouse=True)
def _fake_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test here embeds through FakeProvider (deterministic 8-dim vectors) rather than a
    real network call — same reasoning app.services.chat's tests patch build_provider."""
    monkeypatch.setattr(
        "app.services.knowledge.build_embedding_provider",
        lambda provider, *, api_key, base_url: FakeProvider(api_key=VALID_KEY),
    )


@pytest.fixture(autouse=True)
def _storage_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Index a file connector against a throwaway directory, not the real uploads volume."""
    monkeypatch.setattr("app.services.knowledge.settings.storage_dir", str(tmp_path))
    monkeypatch.setattr("app.workers.index_connector.settings.storage_dir", str(tmp_path))
    return tmp_path


async def test_indexing_a_file_connector_embeds_and_stores_chunks(
    db: AsyncSession, redis_client: Redis, _storage_dir: Path
) -> None:
    del redis_client
    user, workspace, _model = await _workspace_with_embedding_model(db)
    connector = Connector(
        workspace_id=workspace.id, kind=ConnectorKind.FILE, name="Files", created_by=user.id
    )
    db.add(connector)
    await db.flush()
    storage_key = f"{workspace.id}/connectors/{uuid.uuid4()}"
    path = _storage_dir / storage_key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(("Hello world. " * 100).encode())
    document = ConnectorDocument(
        workspace_id=workspace.id,
        connector_id=connector.id,
        filename="test.txt",
        mime="text/plain",
        storage_key=storage_key,
    )
    db.add(document)
    await db.commit()

    await index_connector({}, str(connector.id), str(workspace.id))

    # index_connector runs in its own, separate session — this test's `db` session still has the
    # pre-index rows cached in its identity map, so a plain db.get() would return those stale
    # in-memory objects rather than re-querying; db.refresh() forces it back to the database.
    await db.refresh(connector)
    await db.refresh(document)
    assert connector.status == ConnectorStatus.READY
    assert connector.error is None
    assert connector.embedding_dim == 8
    assert connector.document_count == 1
    assert connector.chunk_count > 0
    assert connector.last_indexed_at is not None

    chunks = (
        await db.scalars(select(ConnectorChunk).where(ConnectorChunk.connector_id == connector.id))
    ).all()
    assert len(chunks) == connector.chunk_count
    assert all(len(c.embedding) == 8 for c in chunks)
    assert document.status == DocumentStatus.DONE


async def test_indexing_a_website_connector_crawls_and_creates_documents(
    db: AsyncSession, redis_client: Redis, _storage_dir: Path
) -> None:
    del redis_client

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="")
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html><head><title>Home</title></head><body>Hello from the site.</body></html>",
        )

    crawl_module._transport = httpx.MockTransport(handler)
    user, workspace, _model = await _workspace_with_embedding_model(db)
    connector = Connector(
        workspace_id=workspace.id,
        kind=ConnectorKind.WEBSITE,
        name="Site",
        url="https://example.com/",
        created_by=user.id,
    )
    db.add(connector)
    await db.commit()

    await index_connector({}, str(connector.id), str(workspace.id))
    crawl_module._transport = None

    await db.refresh(connector)
    assert connector.status == ConnectorStatus.READY
    assert connector.document_count == 1
    assert connector.chunk_count > 0

    documents = (
        await db.scalars(select(ConnectorDocument).where(ConnectorDocument.connector_id == connector.id))
    ).all()
    assert len(documents) == 1
    assert documents[0].source_url == "https://example.com/"
    assert documents[0].title == "Home"


async def test_a_failing_embedding_credential_marks_the_connector_failed_not_a_crash(
    db: AsyncSession, redis_client: Redis, monkeypatch: pytest.MonkeyPatch, _storage_dir: Path
) -> None:
    del redis_client

    async def _failing_embed(*, model: str, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("the embedding provider blew up")

    class _FailingProvider(FakeProvider):
        embed = staticmethod(_failing_embed)  # type: ignore[assignment]

    monkeypatch.setattr(
        "app.services.knowledge.build_embedding_provider",
        lambda provider, *, api_key, base_url: _FailingProvider(api_key=VALID_KEY),
    )
    user, workspace, _model = await _workspace_with_embedding_model(db)
    connector = Connector(
        workspace_id=workspace.id, kind=ConnectorKind.FILE, name="Files", created_by=user.id
    )
    db.add(connector)
    await db.flush()
    storage_key = f"{workspace.id}/connectors/{uuid.uuid4()}"
    path = _storage_dir / storage_key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"Some text to index.")
    document = ConnectorDocument(
        workspace_id=workspace.id,
        connector_id=connector.id,
        filename="test.txt",
        mime="text/plain",
        storage_key=storage_key,
    )
    db.add(document)
    await db.commit()

    await index_connector({}, str(connector.id), str(workspace.id))  # must not raise

    await db.refresh(connector)
    assert connector.status == ConnectorStatus.FAILED
    assert connector.error is not None and "blew up" in connector.error
    chunks = (
        await db.scalars(select(ConnectorChunk).where(ConnectorChunk.connector_id == connector.id))
    ).all()
    assert chunks == []


async def test_reindexing_a_connector_replaces_its_old_chunks(
    db: AsyncSession, redis_client: Redis, _storage_dir: Path
) -> None:
    del redis_client
    user, workspace, _model = await _workspace_with_embedding_model(db)
    connector = Connector(
        workspace_id=workspace.id, kind=ConnectorKind.FILE, name="Files", created_by=user.id
    )
    db.add(connector)
    await db.flush()
    storage_key = f"{workspace.id}/connectors/{uuid.uuid4()}"
    path = _storage_dir / storage_key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"Original content, indexed once.")
    document = ConnectorDocument(
        workspace_id=workspace.id,
        connector_id=connector.id,
        filename="test.txt",
        mime="text/plain",
        storage_key=storage_key,
    )
    db.add(document)
    await db.commit()

    await index_connector({}, str(connector.id), str(workspace.id))
    first_chunk_ids = {
        c.id
        for c in (
            await db.scalars(select(ConnectorChunk).where(ConnectorChunk.connector_id == connector.id))
        ).all()
    }
    assert first_chunk_ids

    # Reindex, unchanged content — the old chunk rows must not simply accumulate alongside new ones.
    await index_connector({}, str(connector.id), str(workspace.id))
    second_chunks = (
        await db.scalars(select(ConnectorChunk).where(ConnectorChunk.connector_id == connector.id))
    ).all()

    assert first_chunk_ids.isdisjoint({c.id for c in second_chunks})
    assert len(second_chunks) > 0
