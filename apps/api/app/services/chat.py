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
from datetime import UTC, datetime

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import async_session_factory, set_workspace_scope
from app.core.errors import (
    ConversationNotFound,
    ModelDoesNotSupportImages,
    ModelNotFound,
    ProviderDisabled,
)
from app.core.redis import new_redis_client
from app.core.tracing import get_tracer
from app.models import (
    Attachment,
    Conversation,
    ExtractStatus,
    LLMModel,
    Message,
    MessageRole,
    Provider,
    User,
)
from app.providers.base import ChatMessage, Done, ImagePart, LLMProvider, StreamError, TextDelta, Usage
from app.providers.registry import build_provider
from app.services.attachments import attach_to_message, read_attachment_bytes
from app.services.credentials import decrypt_credential_key, get_credential
from app.services.flags import evaluate_flag
from app.services.generations import (
    append_event,
    clear_active_generation,
    get_or_create_generation_id,
    set_active_generation,
)
from app.services.usage import record_usage_event

tracer = get_tracer(__name__)

MAX_TOKENS = 4096
TITLE_MAX_LENGTH = 60

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
) -> Conversation:
    """Start a new, empty conversation pinned to one of the workspace's enabled models."""
    model = await db.scalar(
        select(LLMModel).where(LLMModel.id == model_id, LLMModel.workspace_id == workspace_id)
    )
    if model is None:
        raise ModelNotFound()
    conversation = Conversation(
        workspace_id=workspace_id,
        user_id=user.id,
        model_id=model_id,
        title="New conversation",
        system_prompt=system_prompt,
    )
    db.add(conversation)
    await db.flush()
    return conversation


async def update_conversation_model(
    db: AsyncSession, *, workspace_id: uuid.UUID, conversation_id: uuid.UUID, model_id: uuid.UUID
) -> Conversation:
    """Switch a conversation to a different one of the workspace's enabled models — mid-session,
    not just at the start. Already-sent history isn't rewritten or resent to the new model; only
    the turn that follows the switch goes to it, same as a human switching who they're talking to
    mid-conversation doesn't hand the new person a transcript unless asked.
    """
    conversation = await get_conversation(db, workspace_id=workspace_id, conversation_id=conversation_id)
    model = await db.scalar(
        select(LLMModel).where(LLMModel.id == model_id, LLMModel.workspace_id == workspace_id)
    )
    if model is None:
        raise ModelNotFound()
    conversation.model_id = model_id
    await db.flush()
    # `updated_at`'s onupdate=func.now() runs server-side, so the flush above leaves that
    # attribute expired rather than populated — the route below serializes this object
    # immediately (before the request's own commit), and an expired attribute's implicit lazy
    # load can't run outside an awaited context. Refreshing here resolves it while we can still
    # await it, instead of the caller hitting a MissingGreenlet error trying to read it back.
    await db.refresh(conversation)
    return conversation


async def list_conversations(db: AsyncSession, *, workspace_id: uuid.UUID) -> list[Conversation]:
    """List a workspace's conversations, most recently active first."""
    stmt = (
        select(Conversation)
        .where(Conversation.workspace_id == workspace_id)
        .order_by(Conversation.updated_at.desc())
    )
    return list((await db.scalars(stmt)).all())


async def get_conversation(
    db: AsyncSession, *, workspace_id: uuid.UUID, conversation_id: uuid.UUID
) -> Conversation:
    """Load one conversation by id, scoped to its workspace, or raise if it isn't there."""
    conversation = await db.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id, Conversation.workspace_id == workspace_id
        )
    )
    if conversation is None:
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
    db: AsyncSession, conversation: Conversation, messages: list[Message]
) -> list[ChatMessage]:
    """Translate stored messages into the plain role/content shape every provider adapter takes —
    replaying each past turn exactly as it was actually sent, attachments included.

    `Message.content` only ever holds what the user typed (see _augment_with_attachments) — a
    version of this that replayed straight from `content` would silently forget every attachment
    the moment the conversation moved past the turn it was attached to, which is exactly the bug
    a user hit three times over with attachment text before this fix.
    """
    history = []
    if conversation.system_prompt:
        history.append(ChatMessage(role="system", content=conversation.system_prompt))
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

    history = await _to_chat_history(db, conversation, [*existing_messages])
    text, images = _augment_with_attachments(content, attachments)
    history.append(ChatMessage(role="user", content=text, images=images))
    history = _apply_image_budget(history)

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
        )
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return generation_id


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
) -> None:
    """Stream a reply from the provider, appending each chunk to Redis, then persist the result.

    Owns its own database session and Redis client — it must keep running, and keep those
    connections, independent of whatever HTTP request (if any) is currently watching.
    """
    redis = new_redis_client()
    pubsub = redis.pubsub()
    await pubsub.subscribe(f"gen:{generation_id}:stop")

    text_parts: list[str] = []
    input_tokens: int | None = None
    output_tokens: int | None = None
    finish_reason = "stop"
    error_message: str | None = None
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
            stream = adapter.stream(model=provider_model_id, messages=history, max_tokens=MAX_TOKENS)
            async for chunk in stream:
                stop_signal = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.01)
                if stop_signal is not None:
                    finish_reason = "stopped"
                    break
                if isinstance(chunk, TextDelta):
                    text_parts.append(chunk.text)
                    await append_event(redis, generation_id, "delta", {"text": chunk.text})
                elif isinstance(chunk, Usage):
                    input_tokens, output_tokens = chunk.input_tokens, chunk.output_tokens
                elif isinstance(chunk, Done):
                    finish_reason = chunk.finish_reason
                elif isinstance(chunk, StreamError):
                    error_message = chunk.message
                    break
        except Exception as exc:  # noqa: BLE001 — any transport failure still needs a terminal event
            error_message = f"Streaming failed: {exc}"

        span.set_attribute("finish_reason", finish_reason)
        span.set_attribute("tokens_in", input_tokens or 0)
        span.set_attribute("tokens_out", output_tokens or 0)
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
        if not error_message:
            # A stopped-but-partial reply still used real tokens and is still billable; only an
            # outright failure (never reaching a "done") produces nothing worth metering.
            await db.flush()  # materializes assistant_message.id for the usage event's FK
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

    await pubsub.unsubscribe(f"gen:{generation_id}:stop")
    await pubsub.aclose()  # type: ignore[no-untyped-call]  # redis-py's stubs omit this method's types
    await redis.aclose()
