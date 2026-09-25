"""The chat orchestration pipeline: send -> background generation -> persisted reply.

Routes provider calls through FakeProvider via monkeypatch, same pattern as
test_credentials.py and test_models.py — no real network call happens.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Sequence
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import encrypt_secret
from app.core.errors import (
    AttachmentNotFound,
    ConversationNotFound,
    InsufficientRole,
    ModelDoesNotSupportImages,
    ModelNotFound,
)
from app.memory import mem0
from app.models import (
    Assistant,
    Attachment,
    AttachmentSource,
    FeatureFlag,
    FlagScope,
    LLMModel,
    Message,
    Provider,
    ProviderCredential,
    Role,
    Tool,
    ToolInvocation,
    ToolInvocationStatus,
    ToolKind,
    UsageEvent,
    User,
    Workspace,
    WorkspaceMember,
)
from app.providers.base import (
    ChatMessage,
    Chunk,
    Done,
    ImagePart,
    StreamError,
    TextDelta,
    ToolCall,
    ToolCallRequest,
    ToolDefinition,
    Usage,
)
from app.providers.fake import FAKE_REPLY, VALID_KEY, FakeProvider
from app.services.assistant_runtime import delegate_tool_name
from app.services.assistants import create_assistant, delete_assistant, update_assistant
from app.services.attachments import attach_to_message, list_attachments, save_attachment
from app.services.chat import (
    MAX_TOOL_ITERATIONS,
    create_conversation,
    delete_conversation,
    get_conversation,
    list_conversations,
    list_messages,
    send_message,
    update_conversation,
)
from app.services.flags import set_override
from app.services.generations import read_events, request_stop
from app.services.memory import curated_agent_id, personal_agent_id, set_credential
from app.services.tools import to_tool_definition
from app.tools.base import ToolExecutionResult, ToolImage


class SlowFakeProvider(FakeProvider):
    """Like FakeProvider, but paced — gives a test time to call request_stop mid-stream."""

    async def stream(
        self,
        *,
        model: str,
        messages: Sequence[ChatMessage],
        max_tokens: int,
        tools: Sequence[ToolDefinition] = (),
    ) -> AsyncIterator[Chunk]:
        del tools
        for word in ["one ", "two ", "three ", "four "]:
            await asyncio.sleep(0.05)
            yield TextDelta(text=word)
        yield Usage(input_tokens=1, output_tokens=4)
        yield Done(finish_reason="stop")


class ErrorFakeProvider(FakeProvider):
    """Fails partway through a stream — for proving a failed generation bills nothing."""

    async def stream(
        self,
        *,
        model: str,
        messages: Sequence[ChatMessage],
        max_tokens: int,
        tools: Sequence[ToolDefinition] = (),
    ) -> AsyncIterator[Chunk]:
        del tools
        yield TextDelta(text="partial ")
        yield StreamError(message="the provider disconnected")


def _fake_build_provider(provider: Provider, *, api_key: str, base_url: str | None) -> FakeProvider:
    return FakeProvider(api_key=api_key, base_url=base_url)


class ToolCallingFakeProvider(FakeProvider):
    """Asks for one tool on its first stream() call, then replies normally once the loop feeds
    the result back — see FakeProvider.SCRIPTED_TOOL_CALL."""

    SCRIPTED_TOOL_CALL = ToolCall(id="call_1", name="get_weather", arguments={"city": "Paris"})


class NeverFinishesFakeProvider(FakeProvider):
    """Asks for the same tool again on every single call — never gives a final answer. Exercises
    the agent loop's MAX_TOOL_ITERATIONS cap."""

    async def stream(
        self,
        *,
        model: str,
        messages: Sequence[ChatMessage],
        max_tokens: int,
        tools: Sequence[ToolDefinition] = (),
    ) -> AsyncIterator[Chunk]:
        del model, max_tokens, tools
        self.last_messages = messages
        self._stream_calls += 1
        yield Usage(input_tokens=1, output_tokens=1)
        yield ToolCallRequest(
            calls=(ToolCall(id=f"call_{self._stream_calls}", name="get_weather", arguments={}),)
        )


async def _fake_execute_tool(tool: Tool, arguments: dict[str, object]) -> tuple[ToolExecutionResult, int]:
    """Stands in for app.tools.execute.execute_tool in the agent-loop tests — what actually runs
    a tool is exercised separately, in test_tools.py; these tests are about the loop around it."""
    del tool
    return ToolExecutionResult(ok=True, content=f"It is sunny in {arguments.get('city')}"), 5


async def _failing_execute_tool(tool: Tool, arguments: dict[str, object]) -> tuple[ToolExecutionResult, int]:
    del tool, arguments
    return ToolExecutionResult(ok=False, content="The tool blew up."), 3


async def _enable_tools(db: AsyncSession, redis: Redis, *, workspace_id: uuid.UUID) -> None:
    """Flip the `tools` flag on for one workspace — the lever the tools settings page pulls."""
    flag = await db.scalar(select(FeatureFlag).where(FeatureFlag.key == "tools"))
    assert flag is not None
    await set_override(
        db, redis, flag_id=flag.id, scope=FlagScope.WORKSPACE, scope_id=workspace_id, value=True
    )
    await db.commit()  # this test's `db` session must commit for the client's own connection to see it


async def _enable_memory(db: AsyncSession, redis: Redis, *, workspace_id: uuid.UUID) -> None:
    """Flip the `memory` flag on for one workspace — the lever a memory settings page pulls."""
    flag = await db.scalar(select(FeatureFlag).where(FeatureFlag.key == "memory"))
    assert flag is not None
    await set_override(
        db, redis, flag_id=flag.id, scope=FlagScope.WORKSPACE, scope_id=workspace_id, value=True
    )
    await db.commit()  # this test's `db` session must commit for the client's own connection to see it


async def _add_tool(
    db: AsyncSession, *, workspace_id: uuid.UUID, created_by: uuid.UUID, name: str = "get_weather"
) -> Tool:
    """A minimal enabled tool — its actual execution is stubbed via monkeypatching execute_tool
    in the tests that need it, so this never needs a real Tavily key or HTTP endpoint."""
    tool = Tool(
        workspace_id=workspace_id,
        name=name,
        description="Get the current weather for a city.",
        parameters={"type": "object", "properties": {"city": {"type": "string"}}},
        kind=ToolKind.BUILTIN,
        enabled=True,
        created_by=created_by,
    )
    db.add(tool)
    await db.flush()
    return tool


@pytest.fixture
def tool_calling_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Swap in ToolCallingFakeProvider for this test only."""
    monkeypatch.setattr(
        "app.services.chat.build_provider",
        lambda provider, *, api_key, base_url: ToolCallingFakeProvider(api_key=api_key, base_url=base_url),
    )


@pytest.fixture
def never_finishes_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Swap in NeverFinishesFakeProvider for this test only."""
    monkeypatch.setattr(
        "app.services.chat.build_provider",
        lambda provider, *, api_key, base_url: NeverFinishesFakeProvider(api_key=api_key, base_url=base_url),
    )


@pytest.fixture
def slow_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Swap in SlowFakeProvider for this test only, instead of the default fast FakeProvider."""
    monkeypatch.setattr(
        "app.services.chat.build_provider",
        lambda provider, *, api_key, base_url: SlowFakeProvider(api_key=api_key, base_url=base_url),
    )


@pytest.fixture
def error_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Swap in ErrorFakeProvider for this test only, instead of the default fast FakeProvider."""
    monkeypatch.setattr(
        "app.services.chat.build_provider",
        lambda provider, *, api_key, base_url: ErrorFakeProvider(api_key=api_key, base_url=base_url),
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


async def _add_member(db: AsyncSession, *, workspace_id: uuid.UUID, email: str) -> User:
    """A second workspace member — for the tests that check what one member can and can't see
    of another's conversations."""
    user = User(email=email, password_hash="hashed")
    db.add(user)
    await db.flush()
    db.add(WorkspaceMember(workspace_id=workspace_id, user_id=user.id, role=Role.MEMBER))
    await db.flush()
    return user


async def _upload_image(
    db: AsyncSession, *, workspace_id: uuid.UUID, conversation_id: uuid.UUID, uploaded_by: uuid.UUID
) -> Attachment:
    """Upload a real, tiny PNG through the actual attachment service — not a hand-built row — so
    it goes through _normalize_image and comes out genuinely marked `passthrough`."""
    buffer = BytesIO()
    Image.new("RGB", (10, 10), color=(255, 0, 0)).save(buffer, format="PNG")
    return await save_attachment(
        db,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        uploaded_by=uploaded_by,
        filename="photo.png",
        mime="image/png",
        data=buffer.getvalue(),
    )


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


async def test_update_conversation_model_switches_which_model_is_used(db: AsyncSession) -> None:
    """Switching a conversation's model updates what it resolves to for its next send — the
    mechanism the chat page's "change model mid-session" picker relies on."""
    user, workspace, model = await _workspace_with_model(db)
    other_model = LLMModel(
        workspace_id=workspace.id,
        credential_id=model.credential_id,
        provider_model_id="fake-large",
        display_name="Fake Large",
    )
    db.add(other_model)
    await db.flush()
    conversation = await create_conversation(
        db, workspace_id=workspace.id, user=user, model_id=model.id, system_prompt=None
    )
    await db.commit()

    updated = await update_conversation(
        db,
        workspace_id=workspace.id,
        conversation_id=conversation.id,
        viewer_id=user.id,
        changes={"model_id": other_model.id},
    )

    assert updated.model_id == other_model.id


async def test_update_conversation_model_rejects_a_model_from_another_workspace(
    db: AsyncSession,
) -> None:
    """Same guard as create_conversation: a model id belonging to a different workspace 404s,
    even though the conversation being switched is real."""
    user, workspace, model = await _workspace_with_model(db)
    conversation = await create_conversation(
        db, workspace_id=workspace.id, user=user, model_id=model.id, system_prompt=None
    )
    await db.commit()
    other_workspace = Workspace(slug="other-ws", name="Other", owner_id=user.id)
    db.add(other_workspace)
    await db.flush()
    other_credential = ProviderCredential(
        workspace_id=other_workspace.id,
        provider=Provider.ANTHROPIC,
        label="Other",
        ciphertext=b"\x01",
        nonce=b"\x02" * 12,
        wrapped_key=b"\x03" * 44,
        last4="zz99",
        created_by=user.id,
    )
    db.add(other_credential)
    await db.flush()
    other_model = LLMModel(
        workspace_id=other_workspace.id,
        credential_id=other_credential.id,
        provider_model_id="fake-other",
        display_name="Fake Other",
    )
    db.add(other_model)
    await db.flush()

    with pytest.raises(ModelNotFound):
        await update_conversation(
            db,
            workspace_id=workspace.id,
            conversation_id=conversation.id,
            viewer_id=user.id,
            changes={"model_id": other_model.id},
        )


async def test_delete_conversation_removes_it_and_its_messages(
    db: AsyncSession, redis_client: Redis
) -> None:
    """Deleting a conversation is permanent — it and its messages are both gone afterward, via
    the messages table's own ondelete="CASCADE" foreign key rather than an explicit second delete."""
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
    async for _ in read_events(redis_client, generation_id, block_ms=50):
        pass  # let the background task's own session commit the assistant reply before deleting
    conversation_id = conversation.id

    await delete_conversation(
        db, workspace_id=workspace.id, conversation_id=conversation_id, viewer_id=user.id
    )
    await db.commit()

    with pytest.raises(ConversationNotFound):
        await get_conversation(
            db, workspace_id=workspace.id, conversation_id=conversation_id, viewer_id=user.id
        )
    remaining = (
        await db.scalars(select(Message).where(Message.conversation_id == conversation_id))
    ).all()
    assert remaining == []


async def test_delete_conversation_rejects_one_from_another_workspace(db: AsyncSession) -> None:
    """The same workspace-scoping guard every other conversation lookup here has."""
    user, workspace, model = await _workspace_with_model(db)
    conversation = await create_conversation(
        db, workspace_id=workspace.id, user=user, model_id=model.id, system_prompt=None
    )
    await db.commit()
    other_workspace = Workspace(slug="other-delete", name="Other", owner_id=user.id)
    db.add(other_workspace)
    await db.flush()

    with pytest.raises(ConversationNotFound):
        await delete_conversation(
            db, workspace_id=other_workspace.id, conversation_id=conversation.id, viewer_id=user.id
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

    conversations = await list_conversations(db, workspace_id=workspace_b.id, viewer_id=user.id)

    assert conversations == []


async def test_list_conversations_paginates_newest_first_by_cursor(db: AsyncSession) -> None:
    """A `before` cursor from one page's last row fetches exactly the page after it — what the
    sidebar's lazy loading relies on to fetch its next batch of 30.

    One commit per row: Postgres's `now()` is stable for the whole duration of a transaction, so
    several inserts in one uncommitted transaction would all land the same `updated_at` and make
    DESC order a tie (see test_audit.py's own version of this same pattern).
    """
    user, workspace, model = await _workspace_with_model(db)
    conversations = []
    for _ in range(3):
        conversations.append(
            await create_conversation(
                db, workspace_id=workspace.id, user=user, model_id=model.id, system_prompt=None
            )
        )
        await db.commit()

    first_page = await list_conversations(db, workspace_id=workspace.id, viewer_id=user.id, limit=2)
    assert [c.id for c in first_page] == [conversations[2].id, conversations[1].id]

    second_page = await list_conversations(
        db,
        workspace_id=workspace.id,
        viewer_id=user.id,
        limit=2,
        before=first_page[-1].updated_at.isoformat(),
    )
    assert [c.id for c in second_page] == [conversations[0].id]


async def test_list_conversations_filters_by_title_substring(db: AsyncSession) -> None:
    user, workspace, model = await _workspace_with_model(db)
    matching = await create_conversation(
        db, workspace_id=workspace.id, user=user, model_id=model.id, system_prompt=None
    )
    matching.title = "Weekly planning notes"
    other = await create_conversation(
        db, workspace_id=workspace.id, user=user, model_id=model.id, system_prompt=None
    )
    other.title = "Recipe ideas"
    await db.commit()

    results = await list_conversations(db, workspace_id=workspace.id, viewer_id=user.id, q="planning")

    assert [c.id for c in results] == [matching.id]
    assert other.id not in [c.id for c in results]


async def test_get_conversation_from_another_workspace_is_not_found(db: AsyncSession) -> None:
    """Fetching a real conversation id under the wrong workspace behaves like it doesn't exist."""
    user, workspace, model = await _workspace_with_model(db)
    conversation = await create_conversation(
        db, workspace_id=workspace.id, user=user, model_id=model.id, system_prompt=None
    )
    other_id = uuid.uuid4()

    with pytest.raises(ConversationNotFound):
        await get_conversation(
            db, workspace_id=other_id, conversation_id=conversation.id, viewer_id=user.id
        )


async def test_a_private_conversation_is_invisible_to_other_workspace_members(
    db: AsyncSession,
) -> None:
    """A conversation nobody has shared only shows up for whoever started it — conversations are
    private by default, not just visible to the whole workspace."""
    owner, workspace, model = await _workspace_with_model(db)
    teammate = await _add_member(db, workspace_id=workspace.id, email="teammate1@example.com")
    conversation = await create_conversation(
        db, workspace_id=workspace.id, user=owner, model_id=model.id, system_prompt=None
    )
    await db.commit()

    visible_to_owner = await list_conversations(db, workspace_id=workspace.id, viewer_id=owner.id)
    visible_to_teammate = await list_conversations(db, workspace_id=workspace.id, viewer_id=teammate.id)

    assert conversation.id in {c.id for c in visible_to_owner}
    assert visible_to_teammate == []
    with pytest.raises(ConversationNotFound):
        await get_conversation(
            db, workspace_id=workspace.id, conversation_id=conversation.id, viewer_id=teammate.id
        )


async def test_sharing_a_conversation_makes_it_visible_to_the_rest_of_the_workspace(
    db: AsyncSession,
) -> None:
    """Once its owner shares it, a conversation shows up for (and can be opened by) anyone else
    in the workspace — the "collab" half of sharing."""
    owner, workspace, model = await _workspace_with_model(db)
    teammate = await _add_member(db, workspace_id=workspace.id, email="teammate2@example.com")
    conversation = await create_conversation(
        db, workspace_id=workspace.id, user=owner, model_id=model.id, system_prompt=None
    )
    await db.commit()

    await update_conversation(
        db,
        workspace_id=workspace.id,
        conversation_id=conversation.id,
        viewer_id=owner.id,
        changes={"shared": True},
    )

    visible_to_teammate = await list_conversations(db, workspace_id=workspace.id, viewer_id=teammate.id)
    assert conversation.id in {c.id for c in visible_to_teammate}
    fetched = await get_conversation(
        db, workspace_id=workspace.id, conversation_id=conversation.id, viewer_id=teammate.id
    )
    assert fetched.id == conversation.id


async def test_only_the_owner_can_share_or_unshare_a_conversation(db: AsyncSession) -> None:
    """A collaborator a conversation has been shared with can see and use it, but can't revoke
    (or re-extend) that access for everyone else — only the owner controls the sharing flag."""
    owner, workspace, model = await _workspace_with_model(db)
    teammate = await _add_member(db, workspace_id=workspace.id, email="teammate3@example.com")
    conversation = await create_conversation(
        db, workspace_id=workspace.id, user=owner, model_id=model.id, system_prompt=None
    )
    await db.commit()
    await update_conversation(
        db,
        workspace_id=workspace.id,
        conversation_id=conversation.id,
        viewer_id=owner.id,
        changes={"shared": True},
    )

    with pytest.raises(InsufficientRole):
        await update_conversation(
            db,
            workspace_id=workspace.id,
            conversation_id=conversation.id,
            viewer_id=teammate.id,
            changes={"shared": False},
        )


async def test_only_the_owner_can_delete_a_shared_conversation(db: AsyncSession) -> None:
    """A collaborator can chat in a shared conversation but can't delete it out from under its
    owner."""
    owner, workspace, model = await _workspace_with_model(db)
    teammate = await _add_member(db, workspace_id=workspace.id, email="teammate4@example.com")
    conversation = await create_conversation(
        db, workspace_id=workspace.id, user=owner, model_id=model.id, system_prompt=None
    )
    await db.commit()
    await update_conversation(
        db,
        workspace_id=workspace.id,
        conversation_id=conversation.id,
        viewer_id=owner.id,
        changes={"shared": True},
    )

    with pytest.raises(InsufficientRole):
        await delete_conversation(
            db, workspace_id=workspace.id, conversation_id=conversation.id, viewer_id=teammate.id
        )


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


async def test_send_message_records_a_usage_event_on_completion(
    db: AsyncSession, redis_client: Redis
) -> None:
    """A normal completion appends exactly one usage event, priced the same as the message."""
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
    async for _ in read_events(redis_client, generation_id, block_ms=50):
        pass

    events = (
        await db.scalars(select(UsageEvent).where(UsageEvent.workspace_id == workspace.id))
    ).all()
    assert len(events) == 1
    assert events[0].user_id == user.id
    assert events[0].provider == Provider.ANTHROPIC
    assert events[0].tokens_out == 5
    assert events[0].cost_usd == pytest.approx(2 / 1_000_000 + 10 / 1_000_000)


async def test_stopping_mid_stream_still_records_a_usage_event(
    db: AsyncSession, redis_client: Redis, slow_provider: None
) -> None:
    """A stopped reply still used real tokens — it's billable, not free."""
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
    await asyncio.sleep(0.08)
    await request_stop(redis_client, generation_id)
    async for _ in read_events(redis_client, generation_id, block_ms=50):
        pass

    events = (
        await db.scalars(select(UsageEvent).where(UsageEvent.workspace_id == workspace.id))
    ).all()
    # A row lands either way — FakeProvider only reports usage in its final Usage chunk, so a
    # stop before that chunk arrives means real providers' partial-token accounting isn't
    # exercised here, but the event itself (proof a generation ran and should bill something)
    # still must exist.
    assert len(events) == 1


async def test_a_failed_generation_records_no_usage_event(
    db: AsyncSession, redis_client: Redis, error_provider: None
) -> None:
    """A generation that never reaches 'done' bills nothing — there's no completed reply to price."""
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
    events = [e async for e in read_events(redis_client, generation_id, block_ms=50)]
    assert events[-1].type == "error"

    usage_events = (
        await db.scalars(select(UsageEvent).where(UsageEvent.workspace_id == workspace.id))
    ).all()
    assert usage_events == []


async def test_an_attached_image_reaches_the_provider_as_an_image_part(
    db: AsyncSession, redis_client: Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of native vision support: an image attachment shows up in the payload the
    adapter actually sends, as an ImagePart — not silently dropped, not OCR'd into text."""
    user, workspace, model = await _workspace_with_model(db)
    model.supports_vision = True
    await db.flush()
    conversation = await create_conversation(
        db, workspace_id=workspace.id, user=user, model_id=model.id, system_prompt=None
    )
    await db.commit()
    attachment = await _upload_image(
        db, workspace_id=workspace.id, conversation_id=conversation.id, uploaded_by=user.id
    )
    await db.commit()

    captured: list[FakeProvider] = []

    def _capturing_build_provider(
        provider: Provider, *, api_key: str, base_url: str | None
    ) -> FakeProvider:
        instance = FakeProvider(api_key=api_key, base_url=base_url)
        captured.append(instance)
        return instance

    monkeypatch.setattr("app.services.chat.build_provider", _capturing_build_provider)

    generation_id = await send_message(
        db,
        redis_client,
        workspace_id=workspace.id,
        conversation=conversation,
        content="What's in this image?",
        idempotency_key=None,
        attachment_ids=[attachment.id],
    )
    async for _ in read_events(redis_client, generation_id, block_ms=50):
        pass

    assert len(captured) == 1
    sent_turn = captured[0].last_messages[-1]
    assert len(sent_turn.images) == 1
    assert sent_turn.images[0].mime == "image/png"
    assert isinstance(sent_turn.images[0], ImagePart)


async def test_an_image_on_a_non_vision_model_is_rejected_pre_flight(
    db: AsyncSession, redis_client: Redis
) -> None:
    """The gate raises before anything about the send is persisted — the message insert and the
    attachment linkage both roll back, exactly as if the send had never been attempted, rather
    than the image silently never reaching the model."""
    user, workspace, model = await _workspace_with_model(db)  # supports_vision defaults to False
    conversation = await create_conversation(
        db, workspace_id=workspace.id, user=user, model_id=model.id, system_prompt=None
    )
    await db.commit()
    attachment = await _upload_image(
        db, workspace_id=workspace.id, conversation_id=conversation.id, uploaded_by=user.id
    )
    await db.commit()
    conversation_id, attachment_id = conversation.id, attachment.id  # read before rollback expires them

    with pytest.raises(ModelDoesNotSupportImages):
        await send_message(
            db,
            redis_client,
            workspace_id=workspace.id,
            conversation=conversation,
            content="What's in this image?",
            idempotency_key=None,
            attachment_ids=[attachment_id],
        )
    await db.rollback()

    assert await list_messages(db, conversation_id=conversation_id) == []
    refreshed = await db.get(Attachment, attachment_id)
    assert refreshed is not None
    assert refreshed.message_id is None


async def test_an_attachment_from_an_earlier_turn_is_still_in_history_for_a_later_turn(
    db: AsyncSession, redis_client: Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression this phase fixes: attachment content used to reach the model only on the
    turn it was attached to, because history replay read straight from Message.content, which
    never held it in the first place. A follow-up question would find the model had already
    "forgotten" the file — the same disappointment a real user hit three times over with
    attachment text before this fix. This proves it by inspecting turn 2's actual payload, not
    just turn 1's."""
    user, workspace, model = await _workspace_with_model(db)
    model.supports_vision = True
    await db.flush()
    conversation = await create_conversation(
        db, workspace_id=workspace.id, user=user, model_id=model.id, system_prompt=None
    )
    await db.commit()
    text_attachment = await save_attachment(
        db,
        workspace_id=workspace.id,
        conversation_id=conversation.id,
        uploaded_by=user.id,
        filename="notes.txt",
        mime="text/plain",
        data=b"the secret ingredient is basil",
    )
    image_attachment = await _upload_image(
        db, workspace_id=workspace.id, conversation_id=conversation.id, uploaded_by=user.id
    )
    await db.commit()

    captured: list[FakeProvider] = []

    def _capturing_build_provider(
        provider: Provider, *, api_key: str, base_url: str | None
    ) -> FakeProvider:
        instance = FakeProvider(api_key=api_key, base_url=base_url)
        captured.append(instance)
        return instance

    monkeypatch.setattr("app.services.chat.build_provider", _capturing_build_provider)

    first_id = await send_message(
        db,
        redis_client,
        workspace_id=workspace.id,
        conversation=conversation,
        content="Here's my recipe notes and a photo",
        idempotency_key=None,
        attachment_ids=[text_attachment.id, image_attachment.id],
    )
    async for _ in read_events(redis_client, first_id, block_ms=50):
        pass

    second_id = await send_message(
        db,
        redis_client,
        workspace_id=workspace.id,
        conversation=conversation,
        content="What did I attach earlier?",
        idempotency_key=None,
    )
    async for _ in read_events(redis_client, second_id, block_ms=50):
        pass

    assert len(captured) == 2
    replayed_first_turn = captured[1].last_messages[0]
    assert "the secret ingredient is basil" in replayed_first_turn.content
    assert len(replayed_first_turn.images) == 1


async def test_tools_are_not_offered_when_the_flag_is_off(
    db: AsyncSession, redis_client: Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tool can be fully registered and enabled and still never reach the provider — the
    `tools` flag is the switch, same as `attachments`."""
    user, workspace, model = await _workspace_with_model(db)
    await _add_tool(db, workspace_id=workspace.id, created_by=user.id)
    captured: list[FakeProvider] = []

    def _capturing_build_provider(
        provider: Provider, *, api_key: str, base_url: str | None
    ) -> FakeProvider:
        instance = FakeProvider(api_key=api_key, base_url=base_url)
        captured.append(instance)
        return instance

    monkeypatch.setattr("app.services.chat.build_provider", _capturing_build_provider)
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
    async for _ in read_events(redis_client, generation_id, block_ms=50):
        pass

    assert list(captured[0].last_tools) == []


async def test_enabled_tools_are_offered_when_the_flag_is_on(
    db: AsyncSession, redis_client: Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, workspace, model = await _workspace_with_model(db)
    await _enable_tools(db, redis_client, workspace_id=workspace.id)
    tool = await _add_tool(db, workspace_id=workspace.id, created_by=user.id)
    captured: list[FakeProvider] = []

    def _capturing_build_provider(
        provider: Provider, *, api_key: str, base_url: str | None
    ) -> FakeProvider:
        instance = FakeProvider(api_key=api_key, base_url=base_url)
        captured.append(instance)
        return instance

    monkeypatch.setattr("app.services.chat.build_provider", _capturing_build_provider)
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
    async for _ in read_events(redis_client, generation_id, block_ms=50):
        pass

    assert list(captured[0].last_tools) == [to_tool_definition(tool)]


async def test_an_assistants_instructions_become_the_system_turn(
    db: AsyncSession, redis_client: Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A conversation with an assistant uses its instructions instead of its own system_prompt —
    even when one was also set, the assistant takes over."""
    user, workspace, model = await _workspace_with_model(db)
    assistant = await create_assistant(
        db,
        workspace_id=workspace.id,
        created_by=user,
        name="Weather bot",
        instructions="You only ever talk about the weather.",
        model_id=None,
        tool_ids=[],
    )
    captured: list[FakeProvider] = []

    def _capturing_build_provider(
        provider: Provider, *, api_key: str, base_url: str | None
    ) -> FakeProvider:
        instance = FakeProvider(api_key=api_key, base_url=base_url)
        captured.append(instance)
        return instance

    monkeypatch.setattr("app.services.chat.build_provider", _capturing_build_provider)
    conversation = await create_conversation(
        db,
        workspace_id=workspace.id,
        user=user,
        model_id=model.id,
        system_prompt="This should be ignored.",
        assistant_id=assistant.id,
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
    async for _ in read_events(redis_client, generation_id, block_ms=50):
        pass

    system_turn = captured[0].last_messages[0]
    assert system_turn.role == "system"
    assert system_turn.content == "You only ever talk about the weather."


async def test_an_assistants_tools_narrow_the_offered_set(
    db: AsyncSession, redis_client: Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A conversation with an assistant is offered only that assistant's assigned tools — not
    every enabled tool in the workspace, the way an assistant-less conversation is."""
    user, workspace, model = await _workspace_with_model(db)
    await _enable_tools(db, redis_client, workspace_id=workspace.id)
    assigned_tool = await _add_tool(db, workspace_id=workspace.id, created_by=user.id, name="get_weather")
    other_tool = await _add_tool(db, workspace_id=workspace.id, created_by=user.id, name="get_stock_price")
    assistant = await create_assistant(
        db,
        workspace_id=workspace.id,
        created_by=user,
        name="Weather bot",
        instructions="Only use get_weather.",
        model_id=None,
        tool_ids=[assigned_tool.id],
    )
    del other_tool
    captured: list[FakeProvider] = []

    def _capturing_build_provider(
        provider: Provider, *, api_key: str, base_url: str | None
    ) -> FakeProvider:
        instance = FakeProvider(api_key=api_key, base_url=base_url)
        captured.append(instance)
        return instance

    monkeypatch.setattr("app.services.chat.build_provider", _capturing_build_provider)
    conversation = await create_conversation(
        db, workspace_id=workspace.id, user=user, model_id=model.id, system_prompt=None,
        assistant_id=assistant.id,
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
    async for _ in read_events(redis_client, generation_id, block_ms=50):
        pass

    assert list(captured[0].last_tools) == [to_tool_definition(assigned_tool)]


async def test_editing_an_assistant_is_reflected_on_the_very_next_send(
    db: AsyncSession, redis_client: Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An assistant's instructions are resolved live on every send, not snapshotted onto the
    conversation at creation — editing one reaches a conversation already using it."""
    user, workspace, model = await _workspace_with_model(db)
    assistant = await create_assistant(
        db,
        workspace_id=workspace.id,
        created_by=user,
        name="Bot",
        instructions="Version one.",
        model_id=None,
        tool_ids=[],
    )
    captured: list[FakeProvider] = []

    def _capturing_build_provider(
        provider: Provider, *, api_key: str, base_url: str | None
    ) -> FakeProvider:
        instance = FakeProvider(api_key=api_key, base_url=base_url)
        captured.append(instance)
        return instance

    monkeypatch.setattr("app.services.chat.build_provider", _capturing_build_provider)
    conversation = await create_conversation(
        db, workspace_id=workspace.id, user=user, model_id=model.id, system_prompt=None,
        assistant_id=assistant.id,
    )
    await db.commit()
    generation_id = await send_message(
        db, redis_client, workspace_id=workspace.id, conversation=conversation,
        content="First", idempotency_key=None,
    )
    async for _ in read_events(redis_client, generation_id, block_ms=50):
        pass

    await update_assistant(
        db, workspace_id=workspace.id, assistant_id=assistant.id, changes={"instructions": "Version two."}
    )
    await db.commit()
    generation_id = await send_message(
        db, redis_client, workspace_id=workspace.id, conversation=conversation,
        content="Second", idempotency_key=None,
    )
    async for _ in read_events(redis_client, generation_id, block_ms=50):
        pass

    assert captured[0].last_messages[0].content == "Version one."
    assert captured[1].last_messages[0].content == "Version two."


async def test_memory_is_injected_into_the_system_prompt_when_enabled(
    db: AsyncSession, redis_client: Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, workspace, model = await _workspace_with_model(db)
    await _enable_memory(db, redis_client, workspace_id=workspace.id)
    assistant = await create_assistant(
        db, workspace_id=workspace.id, created_by=user, name="Bot", instructions="Be terse.",
        model_id=None, tool_ids=[], memory_enabled=True,
    )
    await set_credential(db, workspace_id=workspace.id, created_by=user, api_key="m0-test")
    await db.commit()

    async def fake_search(**kwargs: object) -> list[dict[str, object]]:
        del kwargs
        return [
            {
                "id": "m1",
                "memory": "The user prefers metric units.",
                "agent_id": curated_agent_id(workspace.id, assistant.id),
            }
        ]

    monkeypatch.setattr(mem0, "search", fake_search)

    async def fake_add(**kwargs: object) -> bool:
        del kwargs
        return True

    monkeypatch.setattr(mem0, "add", fake_add)

    captured: list[FakeProvider] = []

    def _capturing_build_provider(
        provider: Provider, *, api_key: str, base_url: str | None
    ) -> FakeProvider:
        instance = FakeProvider(api_key=api_key, base_url=base_url)
        captured.append(instance)
        return instance

    monkeypatch.setattr("app.services.chat.build_provider", _capturing_build_provider)
    conversation = await create_conversation(
        db, workspace_id=workspace.id, user=user, model_id=model.id, system_prompt=None,
        assistant_id=assistant.id,
    )
    await db.commit()

    generation_id = await send_message(
        db, redis_client, workspace_id=workspace.id, conversation=conversation,
        content="What units should I use?", idempotency_key=None,
    )
    async for _ in read_events(redis_client, generation_id, block_ms=50):
        pass

    system_turn = captured[0].last_messages[0]
    assert system_turn.role == "system"
    assert "Be terse." in system_turn.content
    assert "The user prefers metric units." in system_turn.content


async def test_memory_writes_only_the_personal_scope_even_for_the_assistants_own_creator(
    db: AsyncSession, redis_client: Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The chatter here *is* the assistant's creator — proving chatting still never reaches the
    curated scope, not even for the one person who's otherwise allowed to edit it directly."""
    user, workspace, model = await _workspace_with_model(db)
    await _enable_memory(db, redis_client, workspace_id=workspace.id)
    assistant = await create_assistant(
        db, workspace_id=workspace.id, created_by=user, name="Bot", instructions="x",
        model_id=None, tool_ids=[], memory_enabled=True,
    )
    await set_credential(db, workspace_id=workspace.id, created_by=user, api_key="m0-test")
    await db.commit()

    async def fake_search(**kwargs: object) -> list[dict[str, object]]:
        del kwargs
        return []

    monkeypatch.setattr(mem0, "search", fake_search)

    captured: dict[str, object] = {}
    write_happened = asyncio.Event()

    async def fake_add(**kwargs: object) -> bool:
        captured.update(kwargs)
        write_happened.set()
        return True

    monkeypatch.setattr(mem0, "add", fake_add)
    monkeypatch.setattr("app.services.chat.build_provider", _fake_build_provider)

    conversation = await create_conversation(
        db, workspace_id=workspace.id, user=user, model_id=model.id, system_prompt=None,
        assistant_id=assistant.id,
    )
    await db.commit()

    generation_id = await send_message(
        db, redis_client, workspace_id=workspace.id, conversation=conversation,
        content="Remember I like tea.", idempotency_key=None,
    )
    async for _ in read_events(redis_client, generation_id, block_ms=50):
        pass
    await asyncio.wait_for(write_happened.wait(), timeout=2)

    assert captured["agent_id"] == personal_agent_id(workspace.id, assistant.id)
    assert captured["user_id"] is not None
    # Plain infer=True — mem0's own classifier, no bias fields — see record_turn's own docstring.
    assert captured["infer"] is True
    assert captured["messages"] == [{"role": "user", "content": "Remember I like tea."}]
    assert "immutable" not in captured  # only a curated add ever sets this


async def test_a_mem0_outage_during_retrieval_does_not_break_the_generation(
    db: AsyncSession, redis_client: Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, workspace, model = await _workspace_with_model(db)
    await _enable_memory(db, redis_client, workspace_id=workspace.id)
    assistant = await create_assistant(
        db, workspace_id=workspace.id, created_by=user, name="Bot", instructions="x",
        model_id=None, tool_ids=[], memory_enabled=True,
    )
    await set_credential(db, workspace_id=workspace.id, created_by=user, api_key="m0-test")
    await db.commit()

    async def failing_search(**kwargs: object) -> None:
        del kwargs
        return None  # what mem0.search() itself returns on any transport failure

    async def failing_add(**kwargs: object) -> bool:
        del kwargs
        return False

    monkeypatch.setattr(mem0, "search", failing_search)
    monkeypatch.setattr(mem0, "add", failing_add)
    monkeypatch.setattr("app.services.chat.build_provider", _fake_build_provider)

    conversation = await create_conversation(
        db, workspace_id=workspace.id, user=user, model_id=model.id, system_prompt=None,
        assistant_id=assistant.id,
    )
    await db.commit()

    generation_id = await send_message(
        db, redis_client, workspace_id=workspace.id, conversation=conversation,
        content="Hi", idempotency_key=None,
    )
    events = [e async for e in read_events(redis_client, generation_id, block_ms=50)]

    assert events[-1].type == "done"


async def test_memory_disabled_on_the_assistant_makes_no_mem0_calls(
    db: AsyncSession, redis_client: Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, workspace, model = await _workspace_with_model(db)
    await _enable_memory(db, redis_client, workspace_id=workspace.id)
    assistant = await create_assistant(
        db, workspace_id=workspace.id, created_by=user, name="Bot", instructions="x",
        model_id=None, tool_ids=[], memory_enabled=False,  # the flag is on; the assistant isn't
    )
    await set_credential(db, workspace_id=workspace.id, created_by=user, api_key="m0-test")
    await db.commit()

    calls: list[str] = []

    async def tracked_search(**kwargs: object) -> list[dict[str, object]]:
        del kwargs
        calls.append("search")
        return []

    async def tracked_add(**kwargs: object) -> bool:
        del kwargs
        calls.append("add")
        return True

    monkeypatch.setattr(mem0, "search", tracked_search)
    monkeypatch.setattr(mem0, "add", tracked_add)
    monkeypatch.setattr("app.services.chat.build_provider", _fake_build_provider)

    conversation = await create_conversation(
        db, workspace_id=workspace.id, user=user, model_id=model.id, system_prompt=None,
        assistant_id=assistant.id,
    )
    await db.commit()
    generation_id = await send_message(
        db, redis_client, workspace_id=workspace.id, conversation=conversation,
        content="Hi", idempotency_key=None,
    )
    async for _ in read_events(redis_client, generation_id, block_ms=50):
        pass
    await asyncio.sleep(0.05)  # let any (incorrectly) scheduled background task run

    assert calls == []


async def test_memory_flag_off_makes_no_mem0_calls_even_when_the_assistant_has_it_on(
    db: AsyncSession, redis_client: Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, workspace, model = await _workspace_with_model(db)
    # `memory` flag is left off entirely.
    assistant = await create_assistant(
        db, workspace_id=workspace.id, created_by=user, name="Bot", instructions="x",
        model_id=None, tool_ids=[], memory_enabled=True,
    )
    await set_credential(db, workspace_id=workspace.id, created_by=user, api_key="m0-test")
    await db.commit()

    calls: list[str] = []

    async def tracked_search(**kwargs: object) -> list[dict[str, object]]:
        del kwargs
        calls.append("search")
        return []

    async def tracked_add(**kwargs: object) -> bool:
        del kwargs
        calls.append("add")
        return True

    monkeypatch.setattr(mem0, "search", tracked_search)
    monkeypatch.setattr(mem0, "add", tracked_add)
    monkeypatch.setattr("app.services.chat.build_provider", _fake_build_provider)

    conversation = await create_conversation(
        db, workspace_id=workspace.id, user=user, model_id=model.id, system_prompt=None,
        assistant_id=assistant.id,
    )
    await db.commit()
    generation_id = await send_message(
        db, redis_client, workspace_id=workspace.id, conversation=conversation,
        content="Hi", idempotency_key=None,
    )
    async for _ in read_events(redis_client, generation_id, block_ms=50):
        pass
    await asyncio.sleep(0.05)

    assert calls == []


async def test_a_tool_call_is_executed_and_fed_back_for_a_final_answer(
    db: AsyncSession, redis_client: Redis, tool_calling_provider: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The model asks for a tool, the loop runs it and feeds the result back, and the provider is
    called again for the actual answer — one generation, one persisted reply, usage from both
    round trips summed into it."""
    user, workspace, model = await _workspace_with_model(db)
    await _enable_tools(db, redis_client, workspace_id=workspace.id)
    tool = await _add_tool(db, workspace_id=workspace.id, created_by=user.id)
    monkeypatch.setattr("app.services.chat.execute_tool", _fake_execute_tool)
    conversation = await create_conversation(
        db, workspace_id=workspace.id, user=user, model_id=model.id, system_prompt=None
    )
    await db.commit()

    generation_id = await send_message(
        db,
        redis_client,
        workspace_id=workspace.id,
        conversation=conversation,
        content="What's the weather in Paris?",
        idempotency_key=None,
    )
    events = [e async for e in read_events(redis_client, generation_id, block_ms=50)]

    event_types = [e.type for e in events]
    assert "tool_call" in event_types
    assert "tool_result" in event_types
    assert event_types[-1] == "done"

    messages = await list_messages(db, conversation_id=conversation.id)
    assert [m.role.value for m in messages] == ["user", "assistant"]
    assistant = messages[1]
    assert assistant.content == "Hello from the fake provider. "  # the second stream() call's reply
    # First call: "What's the weather in Paris?" (5 words) -> input 5, output 0 (a tool request).
    # Second call adds the assistant's (empty) tool-call turn and the tool result "It is sunny in
    # Paris" (5 words) -> input 5+0+5=10, output len(FAKE_REPLY.split(" "))=5. Summed: in=15, out=5.
    assert assistant.tokens_in == 15
    assert assistant.tokens_out == 5

    invocations = (
        await db.scalars(select(ToolInvocation).where(ToolInvocation.message_id == assistant.id))
    ).all()
    assert len(invocations) == 1
    assert invocations[0].tool_id == tool.id
    assert invocations[0].name == "get_weather"
    assert invocations[0].arguments == {"city": "Paris"}
    assert invocations[0].status == ToolInvocationStatus.SUCCESS
    assert invocations[0].result == "It is sunny in Paris"


async def test_a_tool_s_images_are_saved_on_the_reply_but_not_as_uploads(
    db: AsyncSession,
    redis_client: Redis,
    tool_calling_provider: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An image a tool returns is written to disk and recorded against the reply and the call
    that made it — but it's not an upload, so it can't be re-sent as one."""
    user, workspace, model = await _workspace_with_model(db)
    await _enable_tools(db, redis_client, workspace_id=workspace.id)
    await _add_tool(db, workspace_id=workspace.id, created_by=user.id)
    monkeypatch.setattr("app.services.attachments.settings.storage_dir", str(tmp_path))

    async def draws(tool: Tool, arguments: dict[str, object]) -> tuple[ToolExecutionResult, int]:
        del tool, arguments
        note = "[image 1 generated and shown to the user]"
        return ToolExecutionResult(ok=True, content=note, images=(ToolImage("image/png", b"png"),)), 5

    monkeypatch.setattr("app.services.chat.execute_tool", draws)
    conversation = await create_conversation(
        db, workspace_id=workspace.id, user=user, model_id=model.id, system_prompt=None
    )
    await db.commit()

    generation_id = await send_message(
        db, redis_client, workspace_id=workspace.id, conversation=conversation,
        content="Draw Paris", idempotency_key=None,
    )
    events = [e async for e in read_events(redis_client, generation_id, block_ms=50)]

    assert [e.data["image_count"] for e in events if e.type == "tool_result"] == [1]
    messages = await list_messages(db, conversation_id=conversation.id)
    invocation = (
        await db.scalars(select(ToolInvocation).where(ToolInvocation.message_id == messages[1].id))
    ).one()
    image = (await db.scalars(select(Attachment).where(Attachment.tool_invocation_id == invocation.id))).one()
    assert image.source == AttachmentSource.TOOL
    assert image.message_id == messages[1].id
    assert image.uploaded_by == user.id
    assert image.original_filename == "get_weather-1.png"
    assert (tmp_path / image.storage_key).read_bytes() == b"png"
    assert await list_attachments(db, conversation_id=conversation.id) == []
    with pytest.raises(AttachmentNotFound):
        await attach_to_message(
            db, conversation_id=conversation.id, attachment_ids=[image.id], message_id=messages[0].id
        )


async def test_a_failing_tool_returns_an_error_to_the_model_not_the_generation(
    db: AsyncSession, redis_client: Redis, tool_calling_provider: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tool that fails still lets the generation finish normally — the model gets to react to
    the failure (and here, FakeProvider's scripted reply is exactly that reaction) rather than
    the whole reply blowing up."""
    user, workspace, model = await _workspace_with_model(db)
    await _enable_tools(db, redis_client, workspace_id=workspace.id)
    await _add_tool(db, workspace_id=workspace.id, created_by=user.id)
    monkeypatch.setattr("app.services.chat.execute_tool", _failing_execute_tool)
    conversation = await create_conversation(
        db, workspace_id=workspace.id, user=user, model_id=model.id, system_prompt=None
    )
    await db.commit()

    generation_id = await send_message(
        db,
        redis_client,
        workspace_id=workspace.id,
        conversation=conversation,
        content="What's the weather in Paris?",
        idempotency_key=None,
    )
    events = [e async for e in read_events(redis_client, generation_id, block_ms=50)]

    assert events[-1].type == "done"
    tool_result_events = [e for e in events if e.type == "tool_result"]
    assert tool_result_events[0].data["ok"] is False

    messages = await list_messages(db, conversation_id=conversation.id)
    invocation = (
        await db.scalars(select(ToolInvocation).where(ToolInvocation.message_id == messages[1].id))
    ).one()
    assert invocation.status == ToolInvocationStatus.ERROR
    assert invocation.error == "The tool blew up."


async def test_calling_an_unknown_tool_is_an_error_not_a_crash(
    db: AsyncSession, redis_client: Redis, tool_calling_provider: None
) -> None:
    """The model asks for a tool that was never registered (or was since disabled) — a stale
    definition from earlier in a long conversation, not a reason to fail the generation."""
    user, workspace, model = await _workspace_with_model(db)
    await _enable_tools(db, redis_client, workspace_id=workspace.id)
    # Deliberately no tool registered — ToolCallingFakeProvider still asks for "get_weather".
    conversation = await create_conversation(
        db, workspace_id=workspace.id, user=user, model_id=model.id, system_prompt=None
    )
    await db.commit()

    generation_id = await send_message(
        db,
        redis_client,
        workspace_id=workspace.id,
        conversation=conversation,
        content="What's the weather in Paris?",
        idempotency_key=None,
    )
    events = [e async for e in read_events(redis_client, generation_id, block_ms=50)]

    assert events[-1].type == "done"
    messages = await list_messages(db, conversation_id=conversation.id)
    invocation = (
        await db.scalars(select(ToolInvocation).where(ToolInvocation.message_id == messages[1].id))
    ).one()
    assert invocation.tool_id is None
    assert invocation.status == ToolInvocationStatus.ERROR


async def test_the_iteration_cap_stops_a_provider_that_never_finishes(
    db: AsyncSession, redis_client: Redis, never_finishes_provider: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A model that keeps calling tools forever doesn't run forever — it's cut off after
    MAX_TOOL_ITERATIONS round trips, and every call made up to that point is still recorded."""
    user, workspace, model = await _workspace_with_model(db)
    await _enable_tools(db, redis_client, workspace_id=workspace.id)
    await _add_tool(db, workspace_id=workspace.id, created_by=user.id)
    monkeypatch.setattr("app.services.chat.execute_tool", _fake_execute_tool)
    conversation = await create_conversation(
        db, workspace_id=workspace.id, user=user, model_id=model.id, system_prompt=None
    )
    await db.commit()

    generation_id = await send_message(
        db,
        redis_client,
        workspace_id=workspace.id,
        conversation=conversation,
        content="What's the weather?",
        idempotency_key=None,
    )
    events = [e async for e in read_events(redis_client, generation_id, block_ms=50)]

    assert events[-1].type == "error"
    assert len([e for e in events if e.type == "tool_call"]) == MAX_TOOL_ITERATIONS

    messages = await list_messages(db, conversation_id=conversation.id)
    invocations = (
        await db.scalars(select(ToolInvocation).where(ToolInvocation.message_id == messages[1].id))
    ).all()
    assert len(invocations) == MAX_TOOL_ITERATIONS


# --- Delegation ("ask another assistant") -----------------------------------------------------

DELEGATE_INSTRUCTIONS = "You are the research delegate."


class DelegatingFakeProvider(FakeProvider):
    """Branches on what it's actually handed rather than on call count or SCRIPTED_TOOL_CALL,
    since a delegate with no model of its own reuses this exact instance for both the
    orchestrator's own turn(s) and the delegate's turn — nothing about "which call number is
    this" can tell them apart on its own.

    `INNER_TOOL_CALL`, if set, is requested once on the delegate's own turn before it gives its
    final answer — for testing that a delegate's own tools run and get recorded. `FAIL_DELEGATE`,
    if set, makes the delegate's turn fail with a StreamError instead of answering.
    """

    INNER_TOOL_CALL: ToolCall | None = None
    FAIL_DELEGATE = False

    def __init__(self, api_key: str, base_url: str | None = None) -> None:
        super().__init__(api_key=api_key, base_url=base_url)
        # Every stream() call this instance served, as (messages, tools) — lets a test inspect
        # exactly what a specific turn (the orchestrator's, or a delegate's) was offered.
        self.calls: list[tuple[Sequence[ChatMessage], Sequence[ToolDefinition]]] = []

    async def stream(
        self,
        *,
        model: str,
        messages: Sequence[ChatMessage],
        max_tokens: int,
        tools: Sequence[ToolDefinition] = (),
    ) -> AsyncIterator[Chunk]:
        del model, max_tokens
        self.last_messages, self.last_tools = messages, tools
        self.calls.append((messages, tools))
        self._stream_calls += 1
        answered = any(m.role == "tool" for m in messages)

        if messages[0].content == DELEGATE_INSTRUCTIONS:
            if self.FAIL_DELEGATE:
                yield StreamError(message="the delegate's provider disconnected")
                return
            if self.INNER_TOOL_CALL is not None and not answered:
                yield Usage(input_tokens=3, output_tokens=0)
                yield ToolCallRequest(calls=(self.INNER_TOOL_CALL,))
                return
            for word in ["Researcher", "says:", "42"]:
                yield TextDelta(text=word + " ")
            yield Usage(input_tokens=3, output_tokens=4)
            yield Done(finish_reason="stop")
            return

        ask_tools = [t for t in tools if t.name.startswith("ask_")]
        if ask_tools and not answered:
            yield Usage(input_tokens=1, output_tokens=0)
            yield ToolCallRequest(
                calls=(ToolCall(id="call_1", name=ask_tools[0].name, arguments={"task": "Find X"}),)
            )
            return

        for word in FAKE_REPLY.split(" "):
            yield TextDelta(text=word + " ")
        yield Usage(input_tokens=2, output_tokens=len(FAKE_REPLY.split(" ")))
        yield Done(finish_reason="stop")


class SlowDelegatingFakeProvider(DelegatingFakeProvider):
    """Like DelegatingFakeProvider, but paces the delegate's own reply word by word — gives a
    test time to call request_stop while a delegation is actually running."""

    async def stream(
        self,
        *,
        model: str,
        messages: Sequence[ChatMessage],
        max_tokens: int,
        tools: Sequence[ToolDefinition] = (),
    ) -> AsyncIterator[Chunk]:
        del model, max_tokens
        self.last_messages, self.last_tools = messages, tools
        self.calls.append((messages, tools))
        self._stream_calls += 1
        answered = any(m.role == "tool" for m in messages)

        if messages[0].content == DELEGATE_INSTRUCTIONS:
            for word in ["one ", "two ", "three ", "four "]:
                await asyncio.sleep(0.05)
                yield TextDelta(text=word)
            yield Usage(input_tokens=1, output_tokens=4)
            yield Done(finish_reason="stop")
            return

        ask_tools = [t for t in tools if t.name.startswith("ask_")]
        if ask_tools and not answered:
            yield Usage(input_tokens=1, output_tokens=0)
            yield ToolCallRequest(
                calls=(ToolCall(id="call_1", name=ask_tools[0].name, arguments={"task": "Find X"}),)
            )
            return

        for word in FAKE_REPLY.split(" "):
            yield TextDelta(text=word + " ")
        yield Usage(input_tokens=2, output_tokens=len(FAKE_REPLY.split(" ")))
        yield Done(finish_reason="stop")


async def _enable_delegation(db: AsyncSession, redis: Redis, *, workspace_id: uuid.UUID) -> None:
    """Flip the `delegation` flag on for one workspace."""
    flag = await db.scalar(select(FeatureFlag).where(FeatureFlag.key == "delegation"))
    assert flag is not None
    await set_override(
        db, redis, flag_id=flag.id, scope=FlagScope.WORKSPACE, scope_id=workspace_id, value=True
    )
    await db.commit()  # this test's `db` session must commit for the client's own connection to see it


async def _assistant_pair(
    db: AsyncSession, *, workspace_id: uuid.UUID, created_by: User
) -> tuple[Assistant, Assistant]:
    """A "Researcher" assistant and a "Writer" assistant that delegates to it."""
    researcher = await create_assistant(
        db, workspace_id=workspace_id, created_by=created_by, name="Researcher",
        instructions=DELEGATE_INSTRUCTIONS, model_id=None, tool_ids=[],
    )
    orchestrator = await create_assistant(
        db, workspace_id=workspace_id, created_by=created_by, name="Writer", instructions="You write things.",
        model_id=None, tool_ids=[], delegate_ids=[researcher.id],
    )
    await db.flush()
    return researcher, orchestrator


@pytest.fixture
def delegating_provider(monkeypatch: pytest.MonkeyPatch) -> list[DelegatingFakeProvider]:
    """Swap in DelegatingFakeProvider, capturing every instance built — a delegate with its own
    model gets a separate instance from the conversation's own."""
    captured: list[DelegatingFakeProvider] = []

    def _build(provider: Provider, *, api_key: str, base_url: str | None) -> DelegatingFakeProvider:
        instance = DelegatingFakeProvider(api_key=api_key, base_url=base_url)
        captured.append(instance)
        return instance

    monkeypatch.setattr("app.services.chat.build_provider", _build)
    # A delegate resolving its own model/credential builds its adapter in assistant_runtime.py,
    # not chat.py (see build_delegate_specs) — patched separately so that path is faked too.
    monkeypatch.setattr("app.services.assistant_runtime.build_provider", _build)
    return captured


def test_delegate_tool_name_sanitizes_and_dedupes() -> None:
    assert delegate_tool_name("Researcher", set()) == "ask_researcher"
    assert delegate_tool_name("Dr. Smith!!", set()) == "ask_dr_smith"
    assert delegate_tool_name("", set()) == "ask_assistant"
    assert delegate_tool_name("Researcher", {"ask_researcher"}) == "ask_researcher_2"
    assert delegate_tool_name("Researcher", {"ask_researcher", "ask_researcher_2"}) == "ask_researcher_3"


async def test_assistant_can_delegate_to_another_assistant(
    db: AsyncSession, redis_client: Redis, delegating_provider: list[DelegatingFakeProvider]
) -> None:
    """The orchestrator's model calls ask_researcher; the delegate's own answer is fed back and
    used in the final reply — and its text never leaks out as a delta of its own."""
    user, workspace, model = await _workspace_with_model(db)
    await _enable_delegation(db, redis_client, workspace_id=workspace.id)
    _researcher, orchestrator = await _assistant_pair(db, workspace_id=workspace.id, created_by=user)
    conversation = await create_conversation(
        db, workspace_id=workspace.id, user=user, model_id=model.id, system_prompt=None,
        assistant_id=orchestrator.id,
    )
    await db.commit()

    generation_id = await send_message(
        db, redis_client, workspace_id=workspace.id, conversation=conversation,
        content="Hi", idempotency_key=None,
    )
    events = [e async for e in read_events(redis_client, generation_id, block_ms=50)]

    tool_calls = [e for e in events if e.type == "tool_call"]
    tool_results = [e for e in events if e.type == "tool_result"]
    assert [e.data["name"] for e in tool_calls] == ["ask_researcher"]
    assert tool_results[0].data["ok"] is True
    assert tool_results[0].data["content"] == "Researcher says: 42 "
    deltas = [e for e in events if e.type == "delta"]
    assert not any("Researcher" in e.data["text"] for e in deltas)
    assert events[-1].type == "done"

    messages = await list_messages(db, conversation_id=conversation.id)
    assistant_message = messages[1]
    assert assistant_message.content.strip() == FAKE_REPLY

    invocations = (
        await db.scalars(select(ToolInvocation).where(ToolInvocation.message_id == assistant_message.id))
    ).all()
    assert len(invocations) == 1
    assert invocations[0].tool_id is None
    assert invocations[0].name == "ask_researcher"
    assert invocations[0].arguments == {"task": "Find X"}
    assert invocations[0].status == ToolInvocationStatus.SUCCESS


async def test_a_delegates_own_tool_calls_are_recorded_nested_under_it(
    db: AsyncSession,
    redis_client: Redis,
    monkeypatch: pytest.MonkeyPatch,
    delegating_provider: list[DelegatingFakeProvider],
) -> None:
    """A delegate calling its own tool shows up as a separate, name-prefixed invocation — visible
    in the transcript as nested under the delegation that triggered it."""
    user, workspace, model = await _workspace_with_model(db)
    await _enable_delegation(db, redis_client, workspace_id=workspace.id)
    await _enable_tools(db, redis_client, workspace_id=workspace.id)
    researcher, orchestrator = await _assistant_pair(db, workspace_id=workspace.id, created_by=user)
    weather_tool = await _add_tool(db, workspace_id=workspace.id, created_by=user.id, name="get_weather")
    await update_assistant(
        db, workspace_id=workspace.id, assistant_id=researcher.id, changes={"tool_ids": [weather_tool.id]}
    )
    await db.commit()
    monkeypatch.setattr("app.services.chat.execute_tool", _fake_execute_tool)
    monkeypatch.setattr(
        DelegatingFakeProvider,
        "INNER_TOOL_CALL",
        ToolCall(id="call_2", name="get_weather", arguments={"city": "Paris"}),
    )

    conversation = await create_conversation(
        db, workspace_id=workspace.id, user=user, model_id=model.id, system_prompt=None,
        assistant_id=orchestrator.id,
    )
    await db.commit()

    generation_id = await send_message(
        db, redis_client, workspace_id=workspace.id, conversation=conversation,
        content="Hi", idempotency_key=None,
    )
    events = [e async for e in read_events(redis_client, generation_id, block_ms=50)]

    assert "ask_researcher/get_weather" in [e.data["name"] for e in events if e.type == "tool_call"]

    messages = await list_messages(db, conversation_id=conversation.id)
    invocations = (
        await db.scalars(select(ToolInvocation).where(ToolInvocation.message_id == messages[1].id))
    ).all()
    by_name = {i.name: i for i in invocations}
    assert set(by_name) == {"ask_researcher", "ask_researcher/get_weather"}
    assert by_name["ask_researcher/get_weather"].tool_id == weather_tool.id
    assert by_name["ask_researcher/get_weather"].status == ToolInvocationStatus.SUCCESS


async def test_a_delegate_on_its_own_model_bills_its_own_usage_event(
    db: AsyncSession, redis_client: Redis, delegating_provider: list[DelegatingFakeProvider]
) -> None:
    """A delegate with its own model gets its own UsageEvent, priced on its own model, attached
    to the outer message — and the outer message's own tokens_in/out stay outer-only."""
    user, workspace, model = await _workspace_with_model(db)
    await _enable_delegation(db, redis_client, workspace_id=workspace.id)
    researcher, orchestrator = await _assistant_pair(db, workspace_id=workspace.id, created_by=user)
    research_model = LLMModel(
        workspace_id=workspace.id, credential_id=model.credential_id, provider_model_id="fake-large",
        display_name="Fake Large", cost_per_mtok_in=5.0, cost_per_mtok_out=10.0,
    )
    db.add(research_model)
    await db.flush()
    await update_assistant(
        db, workspace_id=workspace.id, assistant_id=researcher.id, changes={"model_id": research_model.id}
    )
    conversation = await create_conversation(
        db, workspace_id=workspace.id, user=user, model_id=model.id, system_prompt=None,
        assistant_id=orchestrator.id,
    )
    await db.commit()

    generation_id = await send_message(
        db, redis_client, workspace_id=workspace.id, conversation=conversation,
        content="Hi", idempotency_key=None,
    )
    async for _ in read_events(redis_client, generation_id, block_ms=50):
        pass

    messages = await list_messages(db, conversation_id=conversation.id)
    assistant_message = messages[1]
    events = (
        await db.scalars(select(UsageEvent).where(UsageEvent.message_id == assistant_message.id))
    ).all()
    by_model = {e.model_id: e for e in events}
    assert set(by_model) == {model.id, research_model.id}

    outer_event, delegate_event = by_model[model.id], by_model[research_model.id]
    assert delegate_event.tokens_in == 3
    assert delegate_event.tokens_out == 4
    assert delegate_event.cost_usd == pytest.approx(3 / 1_000_000 * 5.0 + 4 / 1_000_000 * 10.0)
    assert assistant_message.tokens_in == outer_event.tokens_in  # outer-only, never summed
    assert assistant_message.cost_usd == pytest.approx(outer_event.cost_usd + delegate_event.cost_usd)


async def test_delegation_flag_off_offers_no_delegate_tools(
    db: AsyncSession, redis_client: Redis, delegating_provider: list[DelegatingFakeProvider]
) -> None:
    """The `delegation` flag defaults off — an assistant's configured delegates aren't offered as
    tools at all unless the flag is explicitly turned on for the workspace."""
    user, workspace, model = await _workspace_with_model(db)
    _researcher, orchestrator = await _assistant_pair(db, workspace_id=workspace.id, created_by=user)
    conversation = await create_conversation(
        db, workspace_id=workspace.id, user=user, model_id=model.id, system_prompt=None,
        assistant_id=orchestrator.id,
    )
    await db.commit()

    generation_id = await send_message(
        db, redis_client, workspace_id=workspace.id, conversation=conversation,
        content="Hi", idempotency_key=None,
    )
    async for _ in read_events(redis_client, generation_id, block_ms=50):
        pass

    assert list(delegating_provider[0].last_tools) == []
    messages = await list_messages(db, conversation_id=conversation.id)
    assert messages[1].content.strip() == FAKE_REPLY  # never asked to delegate — nothing was offered


async def test_a_failing_delegate_is_an_error_result_not_a_failed_generation(
    db: AsyncSession,
    redis_client: Redis,
    monkeypatch: pytest.MonkeyPatch,
    delegating_provider: list[DelegatingFakeProvider],
) -> None:
    """A delegate's own provider failure is reported back to the orchestrator as a failed tool
    call — the outer generation still finishes normally, and only the outer turn is billed."""
    user, workspace, model = await _workspace_with_model(db)
    await _enable_delegation(db, redis_client, workspace_id=workspace.id)
    _researcher, orchestrator = await _assistant_pair(db, workspace_id=workspace.id, created_by=user)
    monkeypatch.setattr(DelegatingFakeProvider, "FAIL_DELEGATE", True)
    conversation = await create_conversation(
        db, workspace_id=workspace.id, user=user, model_id=model.id, system_prompt=None,
        assistant_id=orchestrator.id,
    )
    await db.commit()

    generation_id = await send_message(
        db, redis_client, workspace_id=workspace.id, conversation=conversation,
        content="Hi", idempotency_key=None,
    )
    events = [e async for e in read_events(redis_client, generation_id, block_ms=50)]

    assert events[-1].type == "done"
    tool_results = [e for e in events if e.type == "tool_result"]
    assert tool_results[0].data["ok"] is False
    assert "Delegate 'ask_researcher' failed" in tool_results[0].data["content"]

    messages = await list_messages(db, conversation_id=conversation.id)
    invocation = (
        await db.scalars(select(ToolInvocation).where(ToolInvocation.message_id == messages[1].id))
    ).one()
    assert invocation.status == ToolInvocationStatus.ERROR

    events_billed = (
        await db.scalars(select(UsageEvent).where(UsageEvent.message_id == messages[1].id))
    ).all()
    assert len(events_billed) == 1  # only the outer turn — the failed delegate billed nothing


async def test_a_deleted_delegate_is_no_longer_offered(
    db: AsyncSession, redis_client: Redis, delegating_provider: list[DelegatingFakeProvider]
) -> None:
    """Deleting an assistant that was someone's delegate cascades the join row — the next send
    just doesn't offer it, the same fallback a deleted tool assignment already gets."""
    user, workspace, model = await _workspace_with_model(db)
    await _enable_delegation(db, redis_client, workspace_id=workspace.id)
    researcher, orchestrator = await _assistant_pair(db, workspace_id=workspace.id, created_by=user)
    await delete_assistant(db, workspace_id=workspace.id, assistant_id=researcher.id)
    conversation = await create_conversation(
        db, workspace_id=workspace.id, user=user, model_id=model.id, system_prompt=None,
        assistant_id=orchestrator.id,
    )
    await db.commit()

    generation_id = await send_message(
        db, redis_client, workspace_id=workspace.id, conversation=conversation,
        content="Hi", idempotency_key=None,
    )
    async for _ in read_events(redis_client, generation_id, block_ms=50):
        pass

    assert list(delegating_provider[0].last_tools) == []
    messages = await list_messages(db, conversation_id=conversation.id)
    assert messages[1].content.strip() == FAKE_REPLY


async def test_stop_during_a_running_delegation_aborts_it_without_a_further_provider_call(
    db: AsyncSession, redis_client: Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stop request reaches a delegate's own loop through the same shared stop signal — without
    it, the delegate's inner loop consuming the stop message would let the outer loop carry on
    to another provider call once the delegation returned."""
    captured: list[SlowDelegatingFakeProvider] = []

    def _build(provider: Provider, *, api_key: str, base_url: str | None) -> SlowDelegatingFakeProvider:
        instance = SlowDelegatingFakeProvider(api_key=api_key, base_url=base_url)
        captured.append(instance)
        return instance

    monkeypatch.setattr("app.services.chat.build_provider", _build)
    user, workspace, model = await _workspace_with_model(db)
    await _enable_delegation(db, redis_client, workspace_id=workspace.id)
    _researcher, orchestrator = await _assistant_pair(db, workspace_id=workspace.id, created_by=user)
    conversation = await create_conversation(
        db, workspace_id=workspace.id, user=user, model_id=model.id, system_prompt=None,
        assistant_id=orchestrator.id,
    )
    await db.commit()

    generation_id = await send_message(
        db, redis_client, workspace_id=workspace.id, conversation=conversation,
        content="Hi", idempotency_key=None,
    )
    await asyncio.sleep(0.08)  # let the delegate start streaming its paced reply
    await request_stop(redis_client, generation_id)
    events = [e async for e in read_events(redis_client, generation_id, block_ms=50)]

    assert events[-1].type == "done"
    assert events[-1].data["finish_reason"] == "stopped"
    tool_results = [e for e in events if e.type == "tool_result"]
    assert tool_results[0].data["ok"] is False
    assert tool_results[0].data["content"] == "Delegation was stopped."
    # Exactly two calls: the orchestrator's "ask" turn, and the delegate's one (stopped) turn —
    # never a third, which is what an unshared stop signal would have let happen.
    assert captured[0]._stream_calls == 2


async def test_delegation_depth_is_one(
    db: AsyncSession, redis_client: Redis, delegating_provider: list[DelegatingFakeProvider]
) -> None:
    """A delegate's own configured delegates are never resolved or offered — depth is always
    exactly 1, structurally (DelegateSpec never carries a nested delegate list at all)."""
    user, workspace, model = await _workspace_with_model(db)
    await _enable_delegation(db, redis_client, workspace_id=workspace.id)
    grandchild = await create_assistant(
        db, workspace_id=workspace.id, created_by=user, name="Grandchild", instructions="x",
        model_id=None, tool_ids=[],
    )
    child = await create_assistant(
        db, workspace_id=workspace.id, created_by=user, name="Researcher", instructions=DELEGATE_INSTRUCTIONS,
        model_id=None, tool_ids=[], delegate_ids=[grandchild.id],
    )
    orchestrator = await create_assistant(
        db, workspace_id=workspace.id, created_by=user, name="Writer", instructions="x",
        model_id=None, tool_ids=[], delegate_ids=[child.id],
    )
    conversation = await create_conversation(
        db, workspace_id=workspace.id, user=user, model_id=model.id, system_prompt=None,
        assistant_id=orchestrator.id,
    )
    await db.commit()

    generation_id = await send_message(
        db, redis_client, workspace_id=workspace.id, conversation=conversation,
        content="Hi", idempotency_key=None,
    )
    async for _ in read_events(redis_client, generation_id, block_ms=50):
        pass

    provider = delegating_provider[0]
    childs_turn_tools = next(
        tools for messages, tools in provider.calls if messages[0].content == DELEGATE_INSTRUCTIONS
    )
    assert not any(t.name.startswith("ask_") for t in childs_turn_tools)


async def test_a_delegate_with_memory_enabled_recalls_its_own_scope(
    db: AsyncSession,
    redis_client: Redis,
    monkeypatch: pytest.MonkeyPatch,
    delegating_provider: list[DelegatingFakeProvider],
) -> None:
    """A memory-enabled delegate recalls its own memories, searched with the task text — not the
    outer conversation's message — and chatting through a delegation never writes memory."""
    user, workspace, model = await _workspace_with_model(db)
    await _enable_delegation(db, redis_client, workspace_id=workspace.id)
    await _enable_memory(db, redis_client, workspace_id=workspace.id)
    researcher, orchestrator = await _assistant_pair(db, workspace_id=workspace.id, created_by=user)
    await update_assistant(
        db, workspace_id=workspace.id, assistant_id=researcher.id, changes={"memory_enabled": True}
    )
    await set_credential(db, workspace_id=workspace.id, created_by=user, api_key="m0-test")
    conversation = await create_conversation(
        db, workspace_id=workspace.id, user=user, model_id=model.id, system_prompt=None,
        assistant_id=orchestrator.id,
    )
    await db.commit()

    search_queries: list[str] = []

    async def fake_search(**kwargs: object) -> list[dict[str, object]]:
        search_queries.append(str(kwargs.get("query")))
        return []

    monkeypatch.setattr(mem0, "search", fake_search)
    add_calls: list[dict[str, object]] = []

    async def fake_add(**kwargs: object) -> bool:
        add_calls.append(kwargs)
        return True

    monkeypatch.setattr(mem0, "add", fake_add)

    generation_id = await send_message(
        db, redis_client, workspace_id=workspace.id, conversation=conversation,
        content="Hi", idempotency_key=None,
    )
    async for _ in read_events(redis_client, generation_id, block_ms=50):
        pass
    await asyncio.sleep(0.05)  # let the outer conversation's own fire-and-forget record_turn run

    assert search_queries == ["Find X"]  # the delegated task, not the outer user message "Hi"
    # The orchestrator has no memory_enabled of its own, so no personal memory is ever written.
    assert add_calls == []
