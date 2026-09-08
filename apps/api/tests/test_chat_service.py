"""The chat orchestration pipeline: send -> background generation -> persisted reply.

Routes provider calls through FakeProvider via monkeypatch, same pattern as
test_credentials.py and test_models.py — no real network call happens.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Sequence
from io import BytesIO

import pytest
from PIL import Image
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import encrypt_secret
from app.core.errors import ConversationNotFound, ModelDoesNotSupportImages, ModelNotFound
from app.models import (
    Attachment,
    LLMModel,
    Message,
    Provider,
    ProviderCredential,
    Role,
    UsageEvent,
    User,
    Workspace,
    WorkspaceMember,
)
from app.providers.base import ChatMessage, Chunk, Done, ImagePart, StreamError, TextDelta, Usage
from app.providers.fake import VALID_KEY, FakeProvider
from app.services.attachments import save_attachment
from app.services.chat import (
    create_conversation,
    delete_conversation,
    get_conversation,
    list_conversations,
    list_messages,
    send_message,
    update_conversation_model,
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


class ErrorFakeProvider(FakeProvider):
    """Fails partway through a stream — for proving a failed generation bills nothing."""

    async def stream(
        self, *, model: str, messages: Sequence[ChatMessage], max_tokens: int
    ) -> AsyncIterator[Chunk]:
        yield TextDelta(text="partial ")
        yield StreamError(message="the provider disconnected")


def _fake_build_provider(provider: Provider, *, api_key: str, base_url: str | None) -> FakeProvider:
    return FakeProvider(api_key=api_key, base_url=base_url)


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

    updated = await update_conversation_model(
        db, workspace_id=workspace.id, conversation_id=conversation.id, model_id=other_model.id
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
        await update_conversation_model(
            db, workspace_id=workspace.id, conversation_id=conversation.id, model_id=other_model.id
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

    await delete_conversation(db, workspace_id=workspace.id, conversation_id=conversation_id)
    await db.commit()

    with pytest.raises(ConversationNotFound):
        await get_conversation(db, workspace_id=workspace.id, conversation_id=conversation_id)
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
            db, workspace_id=other_workspace.id, conversation_id=conversation.id
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
