"""Conversations, messages, and the pipeline that turns a sent message into a streamed reply.

The generation itself runs as a background task, independent of the HTTP request that started
it — per the resumable-streaming design in generations.py, the request's SSE response (and any
later reconnect) just tails the generation's Redis stream, rather than driving the provider call
directly. That keeps a client refreshing mid-reply from losing anything: the background task
keeps writing to Redis (and, at the end, the database) whether or not anyone is watching.
"""

import asyncio
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import async_session_factory, set_workspace_scope
from app.core.errors import (
    ConversationNotFound,
    InsufficientRole,
    ModelDoesNotSupportImages,
    ModelNotFound,
    ProviderDisabled,
)
from app.core.redis import new_redis_client
from app.core.tracing import get_tracer
from app.models import (
    Assistant,
    Attachment,
    Conversation,
    ExtractStatus,
    LLMModel,
    Message,
    MessageRole,
    Provider,
    Tool,
    ToolInvocation,
    ToolInvocationStatus,
    User,
)
from app.providers.base import (
    ChatMessage,
    Done,
    ImagePart,
    LLMProvider,
    StreamError,
    TextDelta,
    ToolCall,
    ToolCallRequest,
    Usage,
)
from app.providers.registry import build_provider
from app.services.assistants import get_assistant, list_assistant_tools
from app.services.attachments import attach_to_message, read_attachment_bytes
from app.services.credentials import decrypt_credential_key, get_credential
from app.services.flags import evaluate_flag
from app.services.generations import (
    append_event,
    clear_active_generation,
    get_or_create_generation_id,
    set_active_generation,
)
from app.services.memory import record_turn, retrieve_for_turn
from app.services.tools import list_enabled_tools, to_tool_definition
from app.services.usage import record_usage_event
from app.tools.execute import execute_tool

tracer = get_tracer(__name__)

MAX_TOKENS = 4096
TITLE_MAX_LENGTH = 60

# A model that keeps calling tools forever is a billing runaway, not a feature — this bounds one
# generation to at most this many round trips through the provider before it's forced to a stop.
MAX_TOOL_ITERATIONS = 5

# Bounds the raw image bytes carried across one request's assembled history. Unlike text, images
# live in `history` as actual bytes for the life of the background generation task (up to the
# stream's timeout) and get re-sent — at real image-token cost — on every subsequent turn. Without
# a cap, a handful of phone photos early in a long conversation would silently balloon every later
# turn's request size and cost.
MAX_HISTORY_IMAGES = 10
MAX_HISTORY_IMAGE_BYTES = 20 * 1024 * 1024

# Holds strong references to in-flight generation tasks so they aren't garbage-collected mid-run
# (asyncio only guarantees a task survives while something still refers to it).
_background_tasks: set[asyncio.Task[None]] = set()


def _heuristic_title(first_message: str) -> str:
    """A conversation's title: the first message, trimmed to a sane length.

    Not LLM-generated — that would need background-worker infrastructure (ARQ) this phase
    doesn't otherwise require. A smarter title is a natural follow-up, not a claim made here.
    """
    trimmed = first_message.strip().split("\n")[0]
    if not trimmed:
        return "New conversation"
    if len(trimmed) <= TITLE_MAX_LENGTH:
        return trimmed
    return trimmed[:TITLE_MAX_LENGTH].rstrip() + "…"


async def create_conversation(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    user: User,
    model_id: uuid.UUID,
    system_prompt: str | None,
    assistant_id: uuid.UUID | None = None,
) -> Conversation:
    """Start a new, empty conversation pinned to one of the workspace's enabled models, and
    optionally governed by one of its saved assistants (see Conversation's own docstring)."""
    model = await db.scalar(
        select(LLMModel).where(LLMModel.id == model_id, LLMModel.workspace_id == workspace_id)
    )
    if model is None:
        raise ModelNotFound()
    if assistant_id is not None:
        await get_assistant(db, workspace_id=workspace_id, assistant_id=assistant_id)  # 404s if not ours
    conversation = Conversation(
        workspace_id=workspace_id,
        user_id=user.id,
        model_id=model_id,
        assistant_id=assistant_id,
        title="New conversation",
        system_prompt=system_prompt,
    )
    db.add(conversation)
    await db.flush()
    return conversation


async def update_conversation(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    conversation_id: uuid.UUID,
    viewer_id: uuid.UUID,
    changes: dict[str, Any],
) -> Conversation:
    """Apply only the fields present in `changes` (built with the request schema's
    `exclude_unset`) — so omitting a field leaves it untouched, but explicitly passing
    `assistant_id: null` clears which assistant governs the chat.

    Switching models (or assistants) is mid-session, not just at the start: already-sent history
    isn't rewritten or resent to the new one, only the turn that follows the switch goes to it,
    same as a human switching who they're talking to mid-conversation doesn't hand the new person
    a transcript unless asked. Available to anyone who can already see this conversation (its
    owner, or anyone it's been shared with) — same as sending a message into it always has been.

    Sharing is different: only the owner may flip it, in either direction. Letting anyone who can
    already see a shared conversation also un-share (or re-share) it would make "shared" a
    one-way ratchet nobody but the very first sharer could undo.
    """
    conversation = await get_conversation(
        db, workspace_id=workspace_id, conversation_id=conversation_id, viewer_id=viewer_id
    )
    if "shared" in changes and conversation.user_id != viewer_id:
        raise InsufficientRole()
    if "model_id" in changes:
        model = await db.scalar(
            select(LLMModel).where(
                LLMModel.id == changes["model_id"], LLMModel.workspace_id == workspace_id
            )
        )
        if model is None:
            raise ModelNotFound()
        conversation.model_id = changes["model_id"]
    if "assistant_id" in changes:
        if changes["assistant_id"] is not None:
            await get_assistant(db, workspace_id=workspace_id, assistant_id=changes["assistant_id"])
        conversation.assistant_id = changes["assistant_id"]
    if "shared" in changes:
        conversation.shared = changes["shared"]
    await db.flush()
    # `updated_at`'s onupdate=func.now() runs server-side, so the flush above leaves that
    # attribute expired rather than populated — the route below serializes this object
    # immediately (before the request's own commit), and an expired attribute's implicit lazy
    # load can't run outside an awaited context. Refreshing here resolves it while we can still
    # await it, instead of the caller hitting a MissingGreenlet error trying to read it back.
    await db.refresh(conversation)
    return conversation


async def delete_conversation(
    db: AsyncSession, *, workspace_id: uuid.UUID, conversation_id: uuid.UUID, viewer_id: uuid.UUID
) -> None:
    """Permanently remove a conversation — its messages, attachments, and usage-event history all
    cascade-delete with it (see each model's ondelete="CASCADE" foreign key). There's no undo and
    no soft-delete here, unlike Workspace: a chat thread has no membership or billing of its own
    to keep around after it's gone, just the history a deleted conversation asks to forget too.

    Owner-only, even once shared: a collaborator who can see and chat in a shared conversation
    still shouldn't be able to delete it out from under whoever started it.
    """
    conversation = await get_conversation(
        db, workspace_id=workspace_id, conversation_id=conversation_id, viewer_id=viewer_id
    )
    if conversation.user_id != viewer_id:
        raise InsufficientRole()
    await db.delete(conversation)
    await db.flush()


async def list_conversations(
    db: AsyncSession, *, workspace_id: uuid.UUID, viewer_id: uuid.UUID
) -> list[Conversation]:
    """List every conversation in the workspace this viewer is allowed to see: their own, plus
    anyone else's that's been explicitly shared — most recently active first."""
    stmt = (
        select(Conversation)
        .where(
            Conversation.workspace_id == workspace_id,
            (Conversation.user_id == viewer_id) | (Conversation.shared.is_(True)),
        )
        .order_by(Conversation.updated_at.desc())
    )
    return list((await db.scalars(stmt)).all())


async def get_conversation(
    db: AsyncSession, *, workspace_id: uuid.UUID, conversation_id: uuid.UUID, viewer_id: uuid.UUID
) -> Conversation:
    """Load one conversation by id, scoped to its workspace — or raise if it isn't there, *or* if
    it is but is private to someone else. Both cases raise the same "not found": whether a
    private conversation exists at all isn't this viewer's business either.
    """
    conversation = await db.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id, Conversation.workspace_id == workspace_id
        )
    )
    if conversation is None:
        raise ConversationNotFound()
    if conversation.user_id != viewer_id and not conversation.shared:
        raise ConversationNotFound()
    return conversation


async def list_messages(db: AsyncSession, *, conversation_id: uuid.UUID) -> list[Message]:
    """List a conversation's messages, oldest first."""
    stmt = (
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at)
    )
    return list((await db.scalars(stmt)).all())


def _augment_with_attachments(
    content: str, attachments: list[Attachment]
) -> tuple[str, tuple[ImagePart, ...]]:
    """What the model actually reads: the user's text, any extracted attachment text folded in,
    and any image attachments as native image parts (see providers/base.py's ImagePart).

    The stored Message keeps just `content` — this augmented version only ever reaches the
    provider call, so the conversation view shows what the user typed, not what the model saw.
    """
    parts = [content]
    images: list[ImagePart] = []
    for attachment in attachments:
        if attachment.extracted_text:
            parts.append(f"[Attached file: {attachment.original_filename}]\n{attachment.extracted_text}")
        elif attachment.extract_status == ExtractStatus.PASSTHROUGH:
            images.append(ImagePart(mime=attachment.mime, data=read_attachment_bytes(attachment)))
    return "\n\n".join(parts), tuple(images)


async def _attachments_by_message_id(
    db: AsyncSession, message_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[Attachment]]:
    """Load every attachment sent with any of these messages, grouped by message id — the join
    _to_chat_history needs to rebuild each past turn exactly as it was actually sent."""
    if not message_ids:
        return {}
    stmt = (
        select(Attachment)
        .where(Attachment.message_id.in_(message_ids))
        .order_by(Attachment.created_at)
    )
    grouped: dict[uuid.UUID, list[Attachment]] = defaultdict(list)
    for attachment in (await db.scalars(stmt)).all():
        assert attachment.message_id is not None  # the query above filtered on exactly that
        grouped[attachment.message_id].append(attachment)
    return grouped


def _apply_image_budget(history: list[ChatMessage]) -> list[ChatMessage]:
    """Cap the raw image bytes carried in one request's assembled history to MAX_HISTORY_IMAGES /
    MAX_HISTORY_IMAGE_BYTES, keeping the most recent images and dropping the oldest first — a
    conversation five turns deep shouldn't still be paying to resend a screenshot from turn one.
    Text extracted from the same attachments is untouched; only the (much heavier) raw image
    bytes are bounded.
    """
    kept_count = 0
    kept_bytes = 0
    result: list[ChatMessage] = []
    for message in reversed(history):
        if not message.images:
            result.append(message)
            continue
        kept_images: list[ImagePart] = []
        for image in reversed(message.images):
            if kept_count >= MAX_HISTORY_IMAGES or kept_bytes + len(image.data) > MAX_HISTORY_IMAGE_BYTES:
                continue
            kept_images.append(image)
            kept_count += 1
            kept_bytes += len(image.data)
        kept_images.reverse()
        result.append(ChatMessage(role=message.role, content=message.content, images=tuple(kept_images)))
    result.reverse()
    return result


async def _to_chat_history(
    db: AsyncSession, messages: list[Message], *, system_prompt: str | None
) -> list[ChatMessage]:
    """Translate stored messages into the plain role/content shape every provider adapter takes —
    replaying each past turn exactly as it was actually sent, attachments included.

    `system_prompt` is resolved by the caller: an assistant's instructions when the conversation
    has one, its own `system_prompt` field otherwise — see send_message.

    `Message.content` only ever holds what the user typed (see _augment_with_attachments) — a
    version of this that replayed straight from `content` would silently forget every attachment
    the moment the conversation moved past the turn it was attached to, which is exactly the bug
    a user hit three times over with attachment text before this fix.
    """
    history = []
    if system_prompt:
        history.append(ChatMessage(role="system", content=system_prompt))
    user_message_ids = [m.id for m in messages if m.role == MessageRole.USER]
    attachments_by_message = await _attachments_by_message_id(db, user_message_ids)
    for m in messages:
        if m.role == MessageRole.USER:
            text, images = _augment_with_attachments(m.content, attachments_by_message.get(m.id, []))
            history.append(ChatMessage(role="user", content=text, images=images))
        elif m.role == MessageRole.ASSISTANT:
            history.append(ChatMessage(role="assistant", content=m.content))
    return history


async def send_message(
    db: AsyncSession,
    redis: Redis,
    *,
    workspace_id: uuid.UUID,
    conversation: Conversation,
    content: str,
    idempotency_key: str | None,
    attachment_ids: list[uuid.UUID] | None = None,
) -> str:
    """Persist the user's message and start (or, for a repeated key, resume) a generation.

    Returns the generation id — the caller tails `gen:{id}` via SSE for the reply.
    """
    generation_id, is_new = await get_or_create_generation_id(redis, idempotency_key)
    if not is_new:
        return generation_id  # a retry of the same send — don't persist or bill twice

    model = await db.get(LLMModel, conversation.model_id)
    assert model is not None  # a conversation can't exist without the model it was created with
    credential = await get_credential(
        db, workspace_id=workspace_id, credential_id=model.credential_id
    )
    provider_enabled = await evaluate_flag(
        db,
        redis,
        key=f"provider.{credential.provider.value}",
        workspace_id=workspace_id,
        user_id=conversation.user_id,
    )
    if not provider_enabled:
        raise ProviderDisabled()

    existing_messages = await list_messages(db, conversation_id=conversation.id)
    is_first_message = not existing_messages

    user_message = Message(conversation_id=conversation.id, role=MessageRole.USER, content=content)
    db.add(user_message)
    await db.flush()  # materializes user_message.id for attach_to_message below — not a commit yet

    attachments: list[Attachment] = []
    if attachment_ids:
        attachments = await attach_to_message(
            db, conversation_id=conversation.id, attachment_ids=attachment_ids, message_id=user_message.id
        )
    if not model.supports_vision and any(
        a.extract_status == ExtractStatus.PASSTHROUGH for a in attachments
    ):
        # Raised pre-flight: nothing above this point has been committed, so get_db()'s
        # rollback-on-exception undoes the message insert and the attachment linkage together,
        # exactly as if this send had never been attempted.
        raise ModelDoesNotSupportImages()

    if is_first_message:
        conversation.title = _heuristic_title(content)
    conversation.updated_at = datetime.now(UTC)
    await db.commit()
    # Committed above (not just flushed): the background task below opens its own session in a
    # separate connection and must be able to see this row (and any attachments just linked to
    # it) the moment it starts. `app.workspace_id` is transaction-local (see set_workspace_scope)
    # and that commit just ended the transaction it was set for — reset it before building
    # history below, which reads attachments back out of the (row-level-security-protected)
    # database via _to_chat_history's join.
    await set_workspace_scope(db, workspace_id)

    # An assistant, if this conversation has one, governs both the system turn and the offered
    # tools — resolved fresh on every send rather than snapshotted, so editing it (or its tools)
    # reaches conversations already using it. `assistant_id` can only ever point at one of this
    # workspace's own assistants (enforced at create/update time), and ON DELETE SET NULL means
    # a since-deleted assistant already shows up here as None, not a dangling reference.
    assistant = await db.get(Assistant, conversation.assistant_id) if conversation.assistant_id else None
    system_prompt = assistant.instructions if assistant is not None else conversation.system_prompt

    # Memory only ever applies to an assistant-backed conversation — there's no "workspace-wide"
    # memory the way there's a workspace-wide tool set, since a memory is meaningless without an
    # assistant to scope it to. `memory_active` gates both directions (recall below, and the
    # post-generation write in _run_generation) on the same two conditions, independent of
    # whether mem0 itself is actually reachable this turn.
    memory_flag_enabled = await evaluate_flag(
        db, redis, key="memory", workspace_id=workspace_id, user_id=conversation.user_id
    )
    memory_active = memory_flag_enabled and assistant is not None and assistant.memory_enabled
    memory_assistant_id: uuid.UUID | None = None
    if memory_active and assistant is not None:
        memory_assistant_id = assistant.id
        memory_block = await retrieve_for_turn(
            db,
            redis,
            workspace_id=workspace_id,
            assistant_id=assistant.id,
            user_id=conversation.user_id,
            query=content,
        )
        if memory_block:
            system_prompt = f"{system_prompt}\n\n{memory_block}" if system_prompt else memory_block

    history = await _to_chat_history(db, [*existing_messages], system_prompt=system_prompt)
    text, images = _augment_with_attachments(content, attachments)
    history.append(ChatMessage(role="user", content=text, images=images))
    history = _apply_image_budget(history)

    tools_enabled = await evaluate_flag(
        db, redis, key="tools", workspace_id=workspace_id, user_id=conversation.user_id
    )
    if not tools_enabled:
        tools: list[Tool] = []
    elif assistant is not None:
        tools = await list_assistant_tools(db, assistant_id=assistant.id)
    else:
        tools = await list_enabled_tools(db, workspace_id=workspace_id)

    api_key = decrypt_credential_key(credential)
    provider = build_provider(credential.provider, api_key=api_key, base_url=credential.base_url)

    await set_active_generation(redis, conversation.id, generation_id)

    task = asyncio.create_task(
        _run_generation(
            generation_id,
            workspace_id=workspace_id,
            user_id=conversation.user_id,
            conversation_id=conversation.id,
            model_id=model.id,
            adapter=provider,
            provider=credential.provider,
            provider_model_id=model.provider_model_id,
            history=history,
            tools=tools,
            user_content=content,
            memory_assistant_id=memory_assistant_id,
        )
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return generation_id


@dataclass
class _ToolInvocationRecord:
    """One tool call from this generation, held in memory until the final assistant message
    exists to attach it to — see the persistence step at the end of _run_generation."""

    tool_id: uuid.UUID | None
    name: str
    arguments: dict[str, object]
    result: str
    status: ToolInvocationStatus
    error: str | None
    latency_ms: int


async def _execute_tool_call(
    redis: Redis, generation_id: str, call: ToolCall, tools_by_name: dict[str, Tool]
) -> _ToolInvocationRecord:
    """Run one requested call, emitting the SSE events either side of it, and return a record
    ready to persist once the generation finishes."""
    await append_event(redis, generation_id, "tool_call", {"name": call.name, "arguments": call.arguments})
    tool = tools_by_name.get(call.name)
    if tool is None:
        # The model asked for a tool that isn't (or is no longer) enabled — a stale definition
        # from earlier in a long conversation, not a reason to fail the whole generation.
        content = f"Tool '{call.name}' is not available."
        await append_event(
            redis, generation_id, "tool_result", {"name": call.name, "ok": False, "content": content}
        )
        return _ToolInvocationRecord(
            tool_id=None,
            name=call.name,
            arguments=call.arguments,
            result=content,
            status=ToolInvocationStatus.ERROR,
            error=content,
            latency_ms=0,
        )
    result, latency_ms = await execute_tool(tool, call.arguments)
    await append_event(
        redis, generation_id, "tool_result", {"name": call.name, "ok": result.ok, "content": result.content}
    )
    return _ToolInvocationRecord(
        tool_id=tool.id,
        name=call.name,
        arguments=call.arguments,
        result=result.content,
        status=ToolInvocationStatus.SUCCESS if result.ok else ToolInvocationStatus.ERROR,
        error=None if result.ok else result.content,
        latency_ms=latency_ms,
    )


async def _run_generation(
    generation_id: str,
    *,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    model_id: uuid.UUID,
    adapter: LLMProvider,
    provider: Provider,
    provider_model_id: str,
    history: list[ChatMessage],
    tools: list[Tool],
    user_content: str,
    memory_assistant_id: uuid.UUID | None = None,
) -> None:
    """Stream a reply from the provider, appending each chunk to Redis, then persist the result.

    When the model calls a tool, this doesn't return — it executes the call, appends the
    assistant's request and the tool's result to the working history, and calls the provider
    again, up to MAX_TOOL_ITERATIONS times. Every iteration's usage adds to the same running
    total, and everything is still one generation, one Redis stream, one persisted reply.

    `memory_assistant_id`, when set, means this turn should be taught to mem0 once it finishes
    successfully — always into that assistant's *personal* scope for `user_id` (see
    services/memory.py's record_turn), fired off after the reply is already visible so a slow or
    unreachable mem0 can never delay it.

    Owns its own database session and Redis client — it must keep running, and keep those
    connections, independent of whatever HTTP request (if any) is currently watching.
    """
    redis = new_redis_client()
    pubsub = redis.pubsub()
    await pubsub.subscribe(f"gen:{generation_id}:stop")

    tool_definitions = [to_tool_definition(t) for t in tools]
    tools_by_name = {t.name: t for t in tools}

    text_parts: list[str] = []
    input_tokens: int | None = None
    output_tokens: int | None = None
    finish_reason = "stop"
    error_message: str | None = None
    stopped = False
    invocations: list[_ToolInvocationRecord] = []
    started_at = time.monotonic()

    with tracer.start_as_current_span(
        "llm.generate",
        attributes={
            "generation_id": generation_id,
            "conversation_id": str(conversation_id),
            "model_id": str(model_id),
            "provider": provider.value,
            "provider_model_id": provider_model_id,
        },
    ) as span:
        try:
            for _ in range(MAX_TOOL_ITERATIONS):
                # Checked at the top of every iteration too, not just between provider chunks —
                # tool execution itself can take several seconds, and a stop request shouldn't
                # have to wait for the *next* round trip to the provider to take effect.
                stop_signal = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.01)
                if stop_signal is not None:
                    finish_reason = "stopped"
                    stopped = True
                    break

                requested_calls: tuple[ToolCall, ...] = ()
                iteration_text: list[str] = []
                stream = adapter.stream(
                    model=provider_model_id, messages=history, max_tokens=MAX_TOKENS, tools=tool_definitions
                )
                async for chunk in stream:
                    stop_signal = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.01)
                    if stop_signal is not None:
                        finish_reason = "stopped"
                        stopped = True
                        break
                    if isinstance(chunk, TextDelta):
                        iteration_text.append(chunk.text)
                        text_parts.append(chunk.text)
                        await append_event(redis, generation_id, "delta", {"text": chunk.text})
                    elif isinstance(chunk, ToolCallRequest):
                        requested_calls = chunk.calls
                    elif isinstance(chunk, Usage):
                        input_tokens = (input_tokens or 0) + chunk.input_tokens
                        output_tokens = (output_tokens or 0) + chunk.output_tokens
                    elif isinstance(chunk, Done):
                        finish_reason = chunk.finish_reason
                    elif isinstance(chunk, StreamError):
                        error_message = chunk.message
                        break

                if error_message or stopped:
                    break
                if not requested_calls:
                    break  # a normal final answer — nothing more to do

                history.append(
                    ChatMessage(role="assistant", content="".join(iteration_text), tool_calls=requested_calls)
                )
                for call in requested_calls:
                    record = await _execute_tool_call(redis, generation_id, call, tools_by_name)
                    invocations.append(record)
                    history.append(
                        ChatMessage(
                            role="tool", content=record.result, tool_call_id=call.id, tool_name=call.name
                        )
                    )
                # Loop again: the provider hasn't given a final answer yet, only asked for tools.
            else:
                # Exhausted every iteration without a break — the model never stopped calling
                # tools for a final answer. Doesn't override a real error or an explicit stop.
                if not error_message and not stopped:
                    error_message = "The model kept calling tools without finishing an answer."
        except Exception as exc:  # noqa: BLE001 — any transport failure still needs a terminal event
            error_message = f"Streaming failed: {exc}"

        span.set_attribute("finish_reason", finish_reason)
        span.set_attribute("tokens_in", input_tokens or 0)
        span.set_attribute("tokens_out", output_tokens or 0)
        span.set_attribute("tool_calls", len(invocations))
        if error_message:
            span.set_attribute("error", error_message)

    latency_ms = int((time.monotonic() - started_at) * 1000)

    async with async_session_factory() as db:
        # This connection never went through get_workspace_ctx (it's a background task, not a
        # request) — row-level security would otherwise block its own reads below.
        await set_workspace_scope(db, workspace_id)
        model = await db.get(LLMModel, model_id)
        cost_usd = None
        if model is not None and model.cost_per_mtok_in is not None and model.cost_per_mtok_out is not None:
            cost_usd = (
                (input_tokens or 0) / 1_000_000 * model.cost_per_mtok_in
                + (output_tokens or 0) / 1_000_000 * model.cost_per_mtok_out
            )
        assistant_message = Message(
            conversation_id=conversation_id,
            role=MessageRole.ASSISTANT,
            content="".join(text_parts),
            tokens_in=input_tokens,
            tokens_out=output_tokens,
            cost_usd=cost_usd,
            finish_reason=None if error_message else finish_reason,
            error=error_message,
        )
        db.add(assistant_message)
        conversation = await db.get(Conversation, conversation_id)
        if conversation is not None:
            conversation.updated_at = datetime.now(UTC)
        # Materializes assistant_message.id — needed below whether or not there's an error, since
        # a generation that failed on, say, its third round trip may still have two real tool
        # calls worth recording.
        await db.flush()
        for record in invocations:
            db.add(
                ToolInvocation(
                    workspace_id=workspace_id,
                    message_id=assistant_message.id,
                    tool_id=record.tool_id,
                    name=record.name,
                    arguments=record.arguments,
                    result=record.result,
                    status=record.status,
                    error=record.error,
                    latency_ms=record.latency_ms,
                )
            )
        if not error_message:
            # A stopped-but-partial reply still used real tokens and is still billable; only an
            # outright failure (never reaching a "done") produces nothing worth metering.
            await record_usage_event(
                db,
                workspace_id=workspace_id,
                user_id=user_id,
                conversation_id=conversation_id,
                message_id=assistant_message.id,
                model_id=model_id,
                provider=provider,
                tokens_in=input_tokens or 0,
                tokens_out=output_tokens or 0,
                cost_usd=cost_usd or 0,
                latency_ms=latency_ms,
            )
        await db.commit()

    if error_message:
        await append_event(redis, generation_id, "error", {"message": error_message})
    else:
        await append_event(redis, generation_id, "done", {"finish_reason": finish_reason})
    await clear_active_generation(redis, conversation_id)

    if not error_message and memory_assistant_id is not None:
        # Fire-and-forget, after the reply is already visible to its reader — a slow or
        # unreachable mem0 must never be the reason a reply takes longer to arrive. A failed
        # generation has nothing worth teaching mem0, hence the `not error_message` guard.
        memory_task = asyncio.create_task(
            record_turn(
                workspace_id=workspace_id,
                assistant_id=memory_assistant_id,
                user_id=user_id,
                user_message=user_content,
                assistant_message="".join(text_parts),
            )
        )
        _background_tasks.add(memory_task)
        memory_task.add_done_callback(_background_tasks.discard)

    await pubsub.unsubscribe(f"gen:{generation_id}:stop")
    await pubsub.aclose()  # type: ignore[no-untyped-call]  # redis-py's stubs omit this method's types
    await redis.aclose()
