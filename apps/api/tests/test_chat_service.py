"""The chat orchestration pipeline: send -> background generation -> persisted reply.

Routes provider calls through FakeProvider via monkeypatch, same pattern as
test_credentials.py and test_models.py — no real network call happens.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Sequence

import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import encrypt_secret
from app.core.errors import ConversationNotFound, ModelNotFound
from app.models import LLMModel, Provider, ProviderCredential, Role, User, Workspace, WorkspaceMember
from app.providers.base import ChatMessage, Chunk, Done, TextDelta, Usage
from app.providers.fake import VALID_KEY, FakeProvider
from app.services.chat import (
    create_conversation,
    get_conversation,
    list_conversations,
    list_messages,
    send_message,
)
from app.services.generations import read_events, request_stop


class SlowFakeProvider(FakeProvider):
    """Like FakeProvider, but paced — gives a test time to call request_stop mid-stream."""

    async def stream(
        self, *, model: str, messages: Sequence[ChatMessage], max_tokens: int
    ) -> AsyncIterator[Chunk]:
        for word in ["one ", "two ", "three ", "four "]:
            await asyncio.sleep(0.05)
            yield TextDelta(text=word)
        yield Usage(input_tokens=1, output_tokens=4)
        yield Done(finish_reason="stop")


def _fake_build_provider(provider: Provider, *, api_key: str, base_url: str | None) -> FakeProvider:
    return FakeProvider(api_key=api_key, base_url=base_url)


@pytest.fixture
def slow_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Swap in SlowFakeProvider for this test only, instead of the default fast FakeProvider."""
    monkeypatch.setattr(
        "app.services.chat.build_provider",
        lambda provider, *, api_key, base_url: SlowFakeProvider(api_key=api_key, base_url=base_url),
    )


@pytest.fixture(autouse=True)
def _fake_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.chat.build_provider", _fake_build_provider)


async def _workspace_with_model(db: AsyncSession) -> tuple[User, Workspace, LLMModel]:
    """Build the full chain send_message needs: a user, workspace, credential, and model."""
    user = User(email="owner@example.com", password_hash="hashed")
    db.add(user)
    await db.flush()
    workspace = Workspace(slug="acme", name="Acme", owner_id=user.id)
    db.add(workspace)
    await db.flush()
    db.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role=Role.OWNER))
    # send_message decrypts this for real (the fake provider swap-in happens one level up, at
    # build_provider) — so it has to be genuinely encrypted, not placeholder bytes.
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
    model = LLMModel(
        workspace_id=workspace.id,
        credential_id=credential.id,
        provider_model_id="fake-small",
        display_name="Fake Small",
        cost_per_mtok_in=1.0,
        cost_per_mtok_out=2.0,
    )
    db.add(model)
    await db.flush()
    return user, workspace, model


async def test_create_conversation_rejects_a_model_from_another_workspace(
    db: AsyncSession,
) -> None:
    """A model id that exists, but not in this workspace, is treated as not found."""
    user, _workspace, model = await _workspace_with_model(db)
    other_workspace = Workspace(slug="other", name="Other", owner_id=user.id)
    db.add(other_workspace)
    await db.flush()

    with pytest.raises(ModelNotFound):
        await create_conversation(
            db,
            workspace_id=other_workspace.id,
            user=user,
            model_id=model.id,
            system_prompt=None,
        )


async def test_send_message_persists_the_reply_and_computes_cost(
    db: AsyncSession, redis_client: Redis
) -> None:
    """A full send: the user turn, the streamed reply, its token counts, and cost all land."""
    user, workspace, model = await _workspace_with_model(db)
    conversation = await create_conversation(
        db, workspace_id=workspace.id, user=user, model_id=model.id, system_prompt=None
    )
    await db.commit()

    generation_id = await send_message(
        db,
        redis_client,
        workspace_id=workspace.id,
        conversation=conversation,
        content="Hi there",
        idempotency_key=None,
    )
    events = [e async for e in read_events(redis_client, generation_id, block_ms=50)]

    assert events[-1].type == "done"
    messages = await list_messages(db, conversation_id=conversation.id)
    assert [m.role.value for m in messages] == ["user", "assistant"]
    assistant = messages[1]
    assert assistant.content == "Hello from the fake provider. "
    assert assistant.tokens_in == 2  # FakeProvider counts words in the sent messages
    assert assistant.tokens_out == 5  # "Hello", "from", "the", "fake", "provider."
    # 2 in @ $1/Mtok + 5 out @ $2/Mtok, per-token
    assert assistant.cost_usd == pytest.approx(2 / 1_000_000 + 10 / 1_000_000)


async def test_first_message_sets_the_conversation_title(db: AsyncSession, redis_client: Redis) -> None:
    """The heuristic title comes from the first message, not left as 'New conversation'."""
    user, workspace, model = await _workspace_with_model(db)
    conversation = await create_conversation(
        db, workspace_id=workspace.id, user=user, model_id=model.id, system_prompt=None
    )
    await db.commit()

    generation_id = await send_message(
        db,
        redis_client,
        workspace_id=workspace.id,
        conversation=conversation,
        content="What's the capital of France?",
        idempotency_key=None,
    )
    async for _ in read_events(redis_client, generation_id, block_ms=50):
        pass

    assert conversation.title == "What's the capital of France?"


async def test_repeated_idempotency_key_does_not_duplicate_the_send(
    db: AsyncSession, redis_client: Redis
) -> None:
    """A retried send with the same key attaches to the original generation, not a new one."""
    user, workspace, model = await _workspace_with_model(db)
    conversation = await create_conversation(
        db, workspace_id=workspace.id, user=user, model_id=model.id, system_prompt=None
    )
    await db.commit()
    key = "retry-abc"

    first_id = await send_message(
        db,
        redis_client,
        workspace_id=workspace.id,
        conversation=conversation,
        content="Hi",
        idempotency_key=key,
    )
    second_id = await send_message(
        db,
        redis_client,
        workspace_id=workspace.id,
        conversation=conversation,
        content="Hi",
        idempotency_key=key,
    )
    async for _ in read_events(redis_client, first_id, block_ms=50):
        pass

    assert first_id == second_id
    messages = await list_messages(db, conversation_id=conversation.id)
    assert [m.role.value for m in messages] == ["user", "assistant"]  # not sent twice


async def test_list_conversations_only_returns_the_workspaces_own(db: AsyncSession) -> None:
    """A conversation in workspace B never appears in workspace A's list."""
    user, workspace_a, model_a = await _workspace_with_model(db)
    workspace_b = Workspace(slug="beta", name="Beta", owner_id=user.id)
    db.add(workspace_b)
    await db.flush()
    await create_conversation(
        db, workspace_id=workspace_a.id, user=user, model_id=model_a.id, system_prompt=None
    )

    conversations = await list_conversations(db, workspace_id=workspace_b.id)

    assert conversations == []


async def test_get_conversation_from_another_workspace_is_not_found(db: AsyncSession) -> None:
    """Fetching a real conversation id under the wrong workspace behaves like it doesn't exist."""
    user, workspace, model = await _workspace_with_model(db)
    conversation = await create_conversation(
        db, workspace_id=workspace.id, user=user, model_id=model.id, system_prompt=None
    )
    other_id = uuid.uuid4()

    with pytest.raises(ConversationNotFound):
        await get_conversation(db, workspace_id=other_id, conversation_id=conversation.id)


async def test_stopping_mid_stream_truncates_the_reply(
    db: AsyncSession, redis_client: Redis, slow_provider: None
) -> None:
    """Requesting stop partway through leaves a shorter, 'stopped' reply — not the full one."""
    user, workspace, model = await _workspace_with_model(db)
    conversation = await create_conversation(
        db, workspace_id=workspace.id, user=user, model_id=model.id, system_prompt=None
    )
    await db.commit()

    generation_id = await send_message(
        db,
        redis_client,
        workspace_id=workspace.id,
        conversation=conversation,
        content="Hi",
        idempotency_key=None,
    )
    await asyncio.sleep(0.08)  # let one or two words stream before stopping
    await request_stop(redis_client, generation_id)

    events = [e async for e in read_events(redis_client, generation_id, block_ms=50)]

    assert events[-1].type == "done"
    assert events[-1].data["finish_reason"] == "stopped"
    delta_count = len([e for e in events if e.type == "delta"])
    assert 0 < delta_count < 4  # stopped before all four words streamed
