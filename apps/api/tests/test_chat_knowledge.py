"""ReStore's chat-side wiring: an assistant with a ready connector gets a knowledge block folded
into its system prompt, a `sources` SSE event ahead of the reply, and persisted `MessageSource`
rows a reloaded thread can fetch back — same fixture shape as test_chat_service.py's memory tests.
"""

import uuid
from typing import cast

import pytest
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import encrypt_secret
from app.models import (
    Assistant,
    Connector,
    ConnectorChunk,
    ConnectorDocument,
    ConnectorKind,
    ConnectorStatus,
    FeatureFlag,
    FlagScope,
    KnowledgeSettings,
    LLMModel,
    ModelKind,
    Provider,
    ProviderCredential,
    Role,
    User,
    Workspace,
    WorkspaceMember,
)
from app.providers.fake import VALID_KEY, FakeProvider
from app.services.assistants import create_assistant
from app.services.chat import create_conversation, send_message
from app.services.flags import set_override
from app.services.generations import read_events
from app.services.knowledge import list_message_sources


def _fake_build_provider(provider: Provider, *, api_key: str, base_url: str | None) -> FakeProvider:
    return FakeProvider(api_key=api_key, base_url=base_url)


@pytest.fixture(autouse=True)
def _fake_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.chat.build_provider", _fake_build_provider)


@pytest.fixture(autouse=True)
def _fake_query_embedding(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub retrieve_for_turn's own query embedding call so a perfect match is guaranteed without
    a real embeddings provider round trip."""

    async def _fake_embed_texts(db, *, workspace_id, texts):  # type: ignore[no-untyped-def]
        del db, workspace_id, texts
        return [[1.0, 0.0, 0.0]], None

    monkeypatch.setattr("app.services.knowledge.embed_texts", _fake_embed_texts)


async def _enable_knowledge(db: AsyncSession, redis: Redis, *, workspace_id: uuid.UUID) -> None:
    flag = await db.scalar(select(FeatureFlag).where(FeatureFlag.key == "knowledge"))
    assert flag is not None
    await set_override(
        db, redis, flag_id=flag.id, scope=FlagScope.WORKSPACE, scope_id=workspace_id, value=True
    )
    await db.commit()


async def _workspace_with_ready_connector(
    db: AsyncSession, *, chunk_content: str = "Our refund window is 30 days."
) -> tuple[User, Workspace, LLMModel, Assistant]:
    """A workspace with a chat model, an embedding model set as the knowledge setting, and one
    connector — already READY, attached to a fresh assistant — with a single matching chunk.
    """
    user = User(email=f"kb-{uuid.uuid4().hex[:8]}@example.com", password_hash="hashed")
    db.add(user)
    await db.flush()
    workspace = Workspace(slug=f"kb-{uuid.uuid4().hex[:8]}", name="Kb", owner_id=user.id)
    db.add(workspace)
    await db.flush()
    db.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role=Role.OWNER))
    secret = encrypt_secret(VALID_KEY)
    credential = ProviderCredential(
        workspace_id=workspace.id,
        provider=Provider.ANTHROPIC,
        label="Prod",
        ciphertext=secret.ciphertext,
        nonce=secret.nonce,
        wrapped_key=secret.wrapped_key,
        last4=VALID_KEY[-4:],
        created_by=user.id,
    )
    db.add(credential)
    await db.flush()
    chat_model = LLMModel(
        workspace_id=workspace.id,
        credential_id=credential.id,
        provider_model_id="fake-small",
        display_name="Fake Small",
        kind=ModelKind.CHAT,
    )
    embed_model = LLMModel(
        workspace_id=workspace.id,
        credential_id=credential.id,
        provider_model_id="fake-embed",
        display_name="Fake Embed",
        kind=ModelKind.EMBEDDING,
    )
    db.add(chat_model)
    db.add(embed_model)
    await db.flush()
    db.add(
        KnowledgeSettings(workspace_id=workspace.id, embedding_model_id=embed_model.id, created_by=user.id)
    )
    connector = Connector(
        workspace_id=workspace.id,
        kind=ConnectorKind.FILE,
        name="Policies",
        status=ConnectorStatus.READY,
        embedding_model_id=embed_model.id,
        created_by=user.id,
    )
    db.add(connector)
    await db.flush()
    document = ConnectorDocument(
        workspace_id=workspace.id,
        connector_id=connector.id,
        filename="policy.txt",
        mime="text/plain",
        title="Refund Policy",
    )
    db.add(document)
    await db.flush()
    db.add(
        ConnectorChunk(
            workspace_id=workspace.id,
            connector_id=connector.id,
            document_id=document.id,
            ordinal=0,
            content=chunk_content,
            embedding=[1.0, 0.0, 0.0],
        )
    )
    await db.flush()
    assistant = await create_assistant(
        db, workspace_id=workspace.id, created_by=user, name="Support", instructions="Be terse.",
        model_id=chat_model.id, tool_ids=[], connector_ids=[connector.id],
    )
    return user, workspace, chat_model, assistant


async def test_system_prompt_includes_relevant_knowledge_when_enabled(
    db: AsyncSession, redis_client: Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, workspace, chat_model, assistant = await _workspace_with_ready_connector(db)
    await _enable_knowledge(db, redis_client, workspace_id=workspace.id)

    captured: list[FakeProvider] = []

    def _capturing_build_provider(
        provider: Provider, *, api_key: str, base_url: str | None
    ) -> FakeProvider:
        instance = FakeProvider(api_key=api_key, base_url=base_url)
        captured.append(instance)
        return instance

    monkeypatch.setattr("app.services.chat.build_provider", _capturing_build_provider)

    conversation = await create_conversation(
        db, workspace_id=workspace.id, user=user, model_id=chat_model.id, system_prompt=None,
        assistant_id=assistant.id,
    )
    await db.commit()

    generation_id = await send_message(
        db, redis_client, workspace_id=workspace.id, conversation=conversation,
        content="What is the refund window?", idempotency_key=None,
    )
    async for _ in read_events(redis_client, generation_id, block_ms=50):
        pass

    system_turn = captured[0].last_messages[0]
    assert system_turn.role == "system"
    assert "Relevant knowledge:" in system_turn.content
    assert "Our refund window is 30 days." in system_turn.content


async def test_sources_event_is_emitted_before_the_first_delta(
    db: AsyncSession, redis_client: Redis
) -> None:
    user, workspace, chat_model, assistant = await _workspace_with_ready_connector(db)
    await _enable_knowledge(db, redis_client, workspace_id=workspace.id)

    conversation = await create_conversation(
        db, workspace_id=workspace.id, user=user, model_id=chat_model.id, system_prompt=None,
        assistant_id=assistant.id,
    )
    await db.commit()

    generation_id = await send_message(
        db, redis_client, workspace_id=workspace.id, conversation=conversation,
        content="What is the refund window?", idempotency_key=None,
    )
    events = [e async for e in read_events(redis_client, generation_id, block_ms=50)]

    event_types = [e.type for e in events]
    assert "sources" in event_types
    sources_index = event_types.index("sources")
    delta_indices = [i for i, t in enumerate(event_types) if t == "delta"]
    assert not delta_indices or sources_index < delta_indices[0]
    sources_payload = cast(list[dict[str, object]], events[sources_index].data["sources"])
    assert len(sources_payload) == 1
    assert sources_payload[0]["label"] == "Refund Policy"


async def test_message_sources_are_persisted_and_listable(
    db: AsyncSession, redis_client: Redis
) -> None:
    user, workspace, chat_model, assistant = await _workspace_with_ready_connector(db)
    await _enable_knowledge(db, redis_client, workspace_id=workspace.id)

    conversation = await create_conversation(
        db, workspace_id=workspace.id, user=user, model_id=chat_model.id, system_prompt=None,
        assistant_id=assistant.id,
    )
    await db.commit()

    generation_id = await send_message(
        db, redis_client, workspace_id=workspace.id, conversation=conversation,
        content="What is the refund window?", idempotency_key=None,
    )
    async for _ in read_events(redis_client, generation_id, block_ms=50):
        pass

    sources = await list_message_sources(db, conversation_id=conversation.id)
    assert len(sources) == 1
    assert sources[0].label == "Refund Policy"
    assert sources[0].score == pytest.approx(1.0)


async def test_knowledge_flag_off_adds_no_block_and_persists_no_sources(
    db: AsyncSession, redis_client: Redis
) -> None:
    user, workspace, chat_model, assistant = await _workspace_with_ready_connector(db)
    # `knowledge` flag is left off entirely.

    conversation = await create_conversation(
        db, workspace_id=workspace.id, user=user, model_id=chat_model.id, system_prompt=None,
        assistant_id=assistant.id,
    )
    await db.commit()

    generation_id = await send_message(
        db, redis_client, workspace_id=workspace.id, conversation=conversation,
        content="What is the refund window?", idempotency_key=None,
    )
    events = [e async for e in read_events(redis_client, generation_id, block_ms=50)]

    assert "sources" not in [e.type for e in events]
    sources = await list_message_sources(db, conversation_id=conversation.id)
    assert sources == []
