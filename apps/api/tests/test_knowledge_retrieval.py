"""retrieve_for_turn's own SQL: nearest chunk first, distance-filtered, and scoped to exactly the
querying assistant's ready, current-embedding-model connectors — inserting chunks with known
vectors directly rather than going through the worker (that's test_index_connector.py's job)."""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import encrypt_secret
from app.models import (
    Assistant,
    AssistantConnector,
    Connector,
    ConnectorChunk,
    ConnectorDocument,
    ConnectorKind,
    ConnectorStatus,
    KnowledgeSettings,
    LLMModel,
    ModelKind,
    Provider,
    ProviderCredential,
    User,
    Workspace,
)
from app.providers.fake import VALID_KEY
from app.services.knowledge import retrieve_for_turn


async def _workspace_with_embedding_model(db: AsyncSession) -> tuple[Workspace, LLMModel, Assistant]:
    user = User(email=f"kr-{uuid.uuid4().hex[:8]}@example.com", password_hash="hashed")
    db.add(user)
    await db.flush()
    workspace = Workspace(slug=f"kr-{uuid.uuid4().hex[:8]}", name="Kr", owner_id=user.id)
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
    assistant = Assistant(
        workspace_id=workspace.id, name="Bot", instructions="x", created_by=user.id
    )
    db.add(assistant)
    await db.flush()
    return workspace, model, assistant


async def _connector_with_chunk(
    db: AsyncSession,
    *,
    workspace: Workspace,
    embedding_model_id: uuid.UUID | None,
    status: ConnectorStatus,
    content: str,
    embedding: list[float],
    user_id: uuid.UUID,
) -> Connector:
    connector = Connector(
        workspace_id=workspace.id,
        kind=ConnectorKind.FILE,
        name=content[:20],
        status=status,
        embedding_model_id=embedding_model_id,
        created_by=user_id,
    )
    db.add(connector)
    await db.flush()
    document = ConnectorDocument(
        workspace_id=workspace.id, connector_id=connector.id, filename="doc.txt", mime="text/plain"
    )
    db.add(document)
    await db.flush()
    db.add(
        ConnectorChunk(
            workspace_id=workspace.id,
            connector_id=connector.id,
            document_id=document.id,
            ordinal=0,
            content=content,
            embedding=embedding,
        )
    )
    await db.flush()
    return connector


@pytest.fixture(autouse=True)
def _fake_query_embedding(monkeypatch: pytest.MonkeyPatch) -> None:
    """retrieve_for_turn embeds the query text itself — stubbed to a fixed vector so each test
    controls exactly which chunks count as "near" without needing a real embedding call."""

    async def _fake_embed_texts(db, *, workspace_id, texts):  # type: ignore[no-untyped-def]
        del db, workspace_id, texts
        return [[1.0, 0.0, 0.0]], None

    monkeypatch.setattr("app.services.knowledge.embed_texts", _fake_embed_texts)


async def test_no_settings_returns_none(db: AsyncSession) -> None:
    user = User(email="nosettings@example.com", password_hash="hashed")
    db.add(user)
    await db.flush()
    workspace = Workspace(slug="nosettings", name="Ns", owner_id=user.id)
    db.add(workspace)
    await db.flush()
    assistant = Assistant(workspace_id=workspace.id, name="Bot", instructions="x", created_by=user.id)
    db.add(assistant)
    await db.commit()

    result = await retrieve_for_turn(
        db, workspace_id=workspace.id, assistant_id=assistant.id, query="anything"
    )

    assert result is None


async def test_nearest_chunk_from_an_attached_ready_connector_is_returned(db: AsyncSession) -> None:
    workspace, model, assistant = await _workspace_with_embedding_model(db)
    connector = await _connector_with_chunk(
        db,
        workspace=workspace,
        embedding_model_id=model.id,
        status=ConnectorStatus.READY,
        content="Paris is the capital of France.",
        embedding=[1.0, 0.0, 0.0],  # identical to the (mocked) query vector -> distance 0
        user_id=assistant.created_by,
    )
    db.add(AssistantConnector(assistant_id=assistant.id, connector_id=connector.id))
    await db.commit()

    result = await retrieve_for_turn(
        db, workspace_id=workspace.id, assistant_id=assistant.id, query="What is the capital of France?"
    )

    assert result is not None
    assert "Relevant knowledge:" in result.block
    assert "Paris is the capital of France." in result.block
    assert len(result.sources) == 1
    assert result.sources[0].connector_id == connector.id
    assert result.sources[0].score == pytest.approx(1.0)


async def test_a_distant_chunk_is_filtered_out_by_max_distance(db: AsyncSession) -> None:
    workspace, model, assistant = await _workspace_with_embedding_model(db)
    connector = await _connector_with_chunk(
        db,
        workspace=workspace,
        embedding_model_id=model.id,
        status=ConnectorStatus.READY,
        content="Tokyo is the capital of Japan.",
        embedding=[0.0, 1.0, 0.0],  # orthogonal to the query vector -> cosine distance 1.0
        user_id=assistant.created_by,
    )
    db.add(AssistantConnector(assistant_id=assistant.id, connector_id=connector.id))
    await db.commit()

    result = await retrieve_for_turn(
        db, workspace_id=workspace.id, assistant_id=assistant.id, query="anything", max_distance=0.55
    )

    assert result is None


async def test_a_connector_not_attached_to_the_assistant_is_ignored(db: AsyncSession) -> None:
    workspace, model, assistant = await _workspace_with_embedding_model(db)
    # A second assistant owns this connector — it's never attached to the one we're querying for.
    other_assistant = Assistant(
        workspace_id=workspace.id, name="Other", instructions="x", created_by=assistant.created_by
    )
    db.add(other_assistant)
    await db.flush()
    connector = await _connector_with_chunk(
        db,
        workspace=workspace,
        embedding_model_id=model.id,
        status=ConnectorStatus.READY,
        content="Not attached to the querying assistant.",
        embedding=[1.0, 0.0, 0.0],
        user_id=assistant.created_by,
    )
    db.add(AssistantConnector(assistant_id=other_assistant.id, connector_id=connector.id))
    await db.commit()

    result = await retrieve_for_turn(
        db, workspace_id=workspace.id, assistant_id=assistant.id, query="anything"
    )

    assert result is None


async def test_a_connector_indexed_under_a_stale_embedding_model_is_skipped(db: AsyncSession) -> None:
    workspace, model, assistant = await _workspace_with_embedding_model(db)
    # A second embedding model in the same workspace, indexed against but no longer the setting.
    stale_model = LLMModel(
        workspace_id=workspace.id,
        credential_id=model.credential_id,
        provider_model_id="fake-embed-old",
        display_name="Old Fake Embed",
        kind=ModelKind.EMBEDDING,
    )
    db.add(stale_model)
    await db.flush()
    connector = await _connector_with_chunk(
        db,
        workspace=workspace,
        embedding_model_id=stale_model.id,
        status=ConnectorStatus.READY,
        content="Indexed under a different embedding model than the workspace uses now.",
        embedding=[1.0, 0.0, 0.0],
        user_id=assistant.created_by,
    )
    db.add(AssistantConnector(assistant_id=assistant.id, connector_id=connector.id))
    await db.commit()

    result = await retrieve_for_turn(
        db, workspace_id=workspace.id, assistant_id=assistant.id, query="anything"
    )

    assert result is None


async def test_a_connector_still_indexing_is_skipped(db: AsyncSession) -> None:
    workspace, model, assistant = await _workspace_with_embedding_model(db)
    connector = await _connector_with_chunk(
        db,
        workspace=workspace,
        embedding_model_id=model.id,
        status=ConnectorStatus.INDEXING,
        content="Not ready yet.",
        embedding=[1.0, 0.0, 0.0],
        user_id=assistant.created_by,
    )
    db.add(AssistantConnector(assistant_id=assistant.id, connector_id=connector.id))
    await db.commit()

    result = await retrieve_for_turn(
        db, workspace_id=workspace.id, assistant_id=assistant.id, query="anything"
    )

    assert result is None


async def test_up_to_two_chunks_from_the_same_document_are_kept(db: AsyncSession) -> None:
    """A near-tie between two of a document's own chunks shouldn't fully exclude either one — see
    knowledge.py's _RETRIEVAL_MAX_CHUNKS_PER_DOCUMENT docstring for the live failure this guards
    against (the chunk with the real answer narrowly losing to a boilerplate one)."""
    workspace, model, assistant = await _workspace_with_embedding_model(db)
    connector = Connector(
        workspace_id=workspace.id,
        kind=ConnectorKind.FILE,
        name="Doc",
        status=ConnectorStatus.READY,
        embedding_model_id=model.id,
        created_by=assistant.created_by,
    )
    db.add(connector)
    await db.flush()
    document = ConnectorDocument(
        workspace_id=workspace.id,
        connector_id=connector.id,
        filename="doc.txt",
        mime="text/plain",
        title="My Document",
    )
    db.add(document)
    await db.flush()
    # Three chunks from the SAME document, all a perfect match — only the cap (2) should surface.
    for ordinal, content in enumerate(["First chunk.", "Second chunk.", "Third chunk."]):
        db.add(
            ConnectorChunk(
                workspace_id=workspace.id, connector_id=connector.id, document_id=document.id,
                ordinal=ordinal, content=content, embedding=[1.0, 0.0, 0.0],
            )
        )
    db.add(AssistantConnector(assistant_id=assistant.id, connector_id=connector.id))
    await db.commit()

    result = await retrieve_for_turn(
        db, workspace_id=workspace.id, assistant_id=assistant.id, query="anything"
    )

    assert result is not None
    assert len(result.sources) == 2
    assert "[Source 1: My Document]" in result.block
    assert "[Source 2: My Document]" in result.block
