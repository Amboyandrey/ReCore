"""Conversations, messages, and the pipeline that turns a sent message into a streamed reply.

The generation itself runs as a background task, independent of the HTTP request that started
it — per the resumable-streaming design in generations.py, the request's SSE response (and any
later reconnect) just tails the generation's Redis stream, rather than driving the provider call
directly. That keeps a client refreshing mid-reply from losing anything: the background task
keeps writing to Redis (and, at the end, the database) whether or not anyone is watching.
"""

import asyncio
import re
import time
import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import async_session_factory, set_workspace_scope
from app.core.errors import (
    ConversationNotFound,
    InsufficientRole,
    InvalidCursor,
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
    ModelKind,
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
    ToolDefinition,
    Usage,
)
from app.providers.registry import build_provider
from app.services.assistants import get_assistant, list_assistant_delegates, list_assistant_tools
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
from app.tools.execute import MAX_RESULT_CHARS, execute_tool

tracer = get_tracer(__name__)

MAX_TOKENS = 4096
TITLE_MAX_LENGTH = 60

# A model that keeps calling tools forever is a billing runaway, not a feature — this bounds one
# generation to at most this many round trips through the provider before it's forced to a stop.
MAX_TOOL_ITERATIONS = 5

# A delegate run is a whole LLM tool-calling loop of its own (up to MAX_TOOL_ITERATIONS provider
# round trips), so it needs far more headroom than execute_tool's own 15s per-call timeout.
DELEGATION_TIMEOUT_SECONDS = 120.0

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
    """Start a new, empty conversation pinned to one of the workspace's enabled chat models, and
    optionally governed by one of its saved assistants (see Conversation's own docstring)."""
    model = await db.scalar(
        select(LLMModel).where(
            LLMModel.id == model_id, LLMModel.workspace_id == workspace_id, LLMModel.kind == ModelKind.CHAT
        )
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
                LLMModel.id == changes["model_id"],
                LLMModel.workspace_id == workspace_id,
                LLMModel.kind == ModelKind.CHAT,
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
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    viewer_id: uuid.UUID,
    limit: int | None = None,
    before: str | None = None,
    q: str | None = None,
) -> list[Conversation]:
    """List conversations in the workspace this viewer is allowed to see: their own, plus anyone
    else's that's been explicitly shared — most recently active first.

    `limit`/`before` cursor-paginate the same way the audit log does: `before` is a previous
    page's last row's `updated_at`, a plain indexed range scan rather than an OFFSET, so a deep
    page costs the same as the first one. `limit=None` (every existing caller before the sidebar
    needed lazy loading) returns everything, unpaginated, exactly as before this parameter
    existed. `q`, if given, filters to titles containing it, case-insensitively.
    """
    stmt = select(Conversation).where(
        Conversation.workspace_id == workspace_id,
        (Conversation.user_id == viewer_id) | (Conversation.shared.is_(True)),
    )
    if q:
        stmt = stmt.where(Conversation.title.ilike(f"%{q}%"))
    if before is not None:
        try:
            cursor = datetime.fromisoformat(before)
        except ValueError as exc:
            raise InvalidCursor() from exc
        stmt = stmt.where(Conversation.updated_at < cursor)
    stmt = stmt.order_by(Conversation.updated_at.desc())
    if limit is not None:
        stmt = stmt.limit(limit)
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

    # A delegate is only ever offered alongside an assistant — there's no "workspace-wide"
    # delegation the way there's a workspace-wide tool set, same reasoning memory follows above.
    delegates: list[DelegateSpec] = []
    if assistant is not None:
        delegation_enabled = await evaluate_flag(
            db, redis, key="delegation", workspace_id=workspace_id, user_id=conversation.user_id
        )
        if delegation_enabled:
            delegates = await _build_delegate_specs(
                db,
                redis,
                workspace_id=workspace_id,
                user_id=conversation.user_id,
                orchestrator=assistant,
                default_adapter=provider,
                default_provider=credential.provider,
                default_model=model,
                tools_enabled=tools_enabled,
                memory_flag_enabled=memory_flag_enabled,
                reserved_names={t.name for t in tools},
            )

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
            delegates=delegates,
        )
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return generation_id


@dataclass
class _DelegateUsage:
    """What a delegate run spent — priced and recorded as its own UsageEvent once the outer
    generation is persisted, since it ran on its own model and possibly its own credential."""

    model_id: uuid.UUID
    provider: Provider
    tokens_in: int
    tokens_out: int
    latency_ms: int


@dataclass
class _ToolInvocationRecord:
    """One tool call from this generation, held in memory until the final assistant message
    exists to attach it to — see the persistence step at the end of _run_generation.

    `children` and `usage` are only ever set on a delegation record: a delegate's own tool calls
    (recorded under it so the transcript shows what it actually did) and what its own LLM usage
    cost, billed separately from the outer turn. A regular tool call's record never has either.
    """

    tool_id: uuid.UUID | None
    name: str
    arguments: dict[str, object]
    result: str
    status: ToolInvocationStatus
    error: str | None
    latency_ms: int
    children: list["_ToolInvocationRecord"] = field(default_factory=list)
    usage: _DelegateUsage | None = None


@dataclass(frozen=True)
class DelegateSpec:
    """One assistant this conversation's assistant may hand a task to — everything needed to run
    its own tool-calling loop, resolved once per send (see _build_delegate_specs) so the loop
    itself never has to touch the database. `tool_name` is what the model actually sees and
    calls; `definition` is the synthesized ToolDefinition offered alongside the real tools."""

    assistant_id: uuid.UUID
    tool_name: str
    definition: ToolDefinition
    instructions: str
    adapter: LLMProvider
    provider: Provider
    model_id: uuid.UUID
    provider_model_id: str
    tools: list[Tool]
    memory_active: bool


_DELEGATE_NAME_SANITIZER = re.compile(r"[^a-zA-Z0-9_-]+")


def delegate_tool_name(name: str, taken: set[str]) -> str:
    """Turn an assistant's display name into a legal, collision-free tool name to offer the
    model — assistant names aren't unique per workspace, but a tool name must be (and must match
    ^[a-zA-Z0-9_-]+$, capped at 64 chars, same as a real tool's — see schemas/tool.py).

    `taken` starts as the orchestrator's own real tool names and grows by one with every delegate
    named — the caller does that, not this function — which is what guarantees an `ask_*` name
    can never collide with a real tool, letting _execute_tool_call check delegates first safely.
    """
    slug = _DELEGATE_NAME_SANITIZER.sub("_", name.strip()).strip("_").lower() or "assistant"
    base = ("ask_" + slug)[:64]
    if base not in taken:
        return base
    suffix = 2
    while True:
        tag = f"_{suffix}"
        candidate = base[: 64 - len(tag)] + tag
        if candidate not in taken:
            return candidate
        suffix += 1


async def _build_delegate_specs(
    db: AsyncSession,
    redis: Redis,
    *,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    orchestrator: Assistant,
    default_adapter: LLMProvider,
    default_provider: Provider,
    default_model: LLMModel,
    tools_enabled: bool,
    memory_flag_enabled: bool,
    reserved_names: set[str],
) -> list[DelegateSpec]:
    """Resolve every assistant `orchestrator` may delegate to into a ready-to-run DelegateSpec —
    its own model/credential/adapter if it has one (falling back to the conversation's), its own
    tools, and whether memory recall applies to it.

    A delegate whose own model's provider has been disabled is skipped outright rather than
    silently run on a different model its author never chose — the same posture ProviderDisabled
    already takes for the conversation's own model. Depth is always 1: this never recurses into a
    delegate's own delegates (list_assistant_delegates is only ever called from here, on
    `orchestrator`, never on a delegate).
    """
    delegates = await list_assistant_delegates(db, assistant_id=orchestrator.id)
    taken = set(reserved_names)
    adapters_by_credential: dict[uuid.UUID, LLMProvider] = {}
    specs: list[DelegateSpec] = []
    for delegate in delegates:
        if delegate.model_id is not None:
            model = await db.get(LLMModel, delegate.model_id)
            if model is None:
                continue
            credential = await get_credential(
                db, workspace_id=workspace_id, credential_id=model.credential_id
            )
            provider_enabled = await evaluate_flag(
                db,
                redis,
                key=f"provider.{credential.provider.value}",
                workspace_id=workspace_id,
                user_id=user_id,
            )
            if not provider_enabled:
                continue
            adapter = adapters_by_credential.get(credential.id)
            if adapter is None:
                adapter = build_provider(
                    credential.provider,
                    api_key=decrypt_credential_key(credential),
                    base_url=credential.base_url,
                )
                adapters_by_credential[credential.id] = adapter
            provider, provider_model_id, model_id = credential.provider, model.provider_model_id, model.id
        else:
            adapter, provider = default_adapter, default_provider
            provider_model_id, model_id = default_model.provider_model_id, default_model.id

        tools = await list_assistant_tools(db, assistant_id=delegate.id) if tools_enabled else []
        tool_name = delegate_tool_name(delegate.name, taken)
        taken.add(tool_name)
        specs.append(
            DelegateSpec(
                assistant_id=delegate.id,
                tool_name=tool_name,
                definition=ToolDefinition(
                    name=tool_name,
                    description=(
                        f'Delegate a task to the "{delegate.name}" assistant and get its answer '
                        "back. Write a complete, self-contained task — it cannot see this "
                        "conversation."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {"task": {"type": "string"}},
                        "required": ["task"],
                    },
                ),
                instructions=delegate.instructions,
                adapter=adapter,
                provider=provider,
                model_id=model_id,
                provider_model_id=provider_model_id,
                tools=tools,
                memory_active=memory_flag_enabled and delegate.memory_enabled,
            )
        )
    return specs


class _StopSignal:
    """A sticky wrapper over one generation's `gen:{id}:stop` pubsub.

    Reading a pubsub message is destructive — if the outer loop and a delegate's own inner loop
    each polled the raw pubsub directly, whichever one happened to poll first would consume the
    stop request and the other would never see it, letting the outer loop carry on to another
    provider call after the user asked to stop. Sharing one `_StopSignal` between them means
    whichever loop is actually running when the stop arrives observes it, and it latches `stopped`
    for both from then on.
    """

    def __init__(self, pubsub: Any) -> None:
        self._pubsub = pubsub
        self.stopped = False

    async def check(self) -> bool:
        if not self.stopped:
            message = await self._pubsub.get_message(ignore_subscribe_messages=True, timeout=0.01)
            if message is not None:
                self.stopped = True
        return self.stopped


@dataclass(frozen=True)
class _LoopContext:
    """Everything the tool-calling loop needs that doesn't change between provider round trips,
    outer turn or delegate run alike — bundled so _run_tool_loop's signature doesn't grow every
    time the loop needs one more ambient thing from its caller."""

    redis: Redis
    generation_id: str
    stop: _StopSignal
    workspace_id: uuid.UUID
    user_id: uuid.UUID


@dataclass
class _LoopResult:
    """What one run through the tool-calling loop produced — shaped identically whether it was
    the outer generation's own turn or a delegate's."""

    text: str
    input_tokens: int | None
    output_tokens: int | None
    finish_reason: str
    error_message: str | None
    stopped: bool
    invocations: list[_ToolInvocationRecord]


async def _run_delegation(ctx: _LoopContext, call: ToolCall, spec: DelegateSpec) -> _ToolInvocationRecord:
    """Run one delegate end to end: recall its own memory if it has any, run its own tool-calling
    loop on the task alone — it never sees the outer conversation — and turn the result into the
    same shape a regular tool call produces, including its own nested ToolInvocation children and
    the usage it billed, both surfaced to _run_generation's persistence step via the return value.
    """
    await append_event(
        ctx.redis, ctx.generation_id, "tool_call", {"name": call.name, "arguments": call.arguments}
    )

    task = call.arguments.get("task")
    if not isinstance(task, str) or not task.strip():
        content = f"Delegating to '{call.name}' needs a non-empty \"task\" argument."
        await append_event(
            ctx.redis, ctx.generation_id, "tool_result", {"name": call.name, "ok": False, "content": content}
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

    system_prompt = spec.instructions
    if spec.memory_active:
        # A short-lived session, opened and closed before the (possibly long) delegate LLM loop
        # below runs — not held for the duration of it, the same reasoning _run_generation's own
        # persistence session only opens after the stream finishes.
        async with async_session_factory() as memory_db:
            await set_workspace_scope(memory_db, ctx.workspace_id)
            memory_block = await retrieve_for_turn(
                memory_db,
                workspace_id=ctx.workspace_id,
                assistant_id=spec.assistant_id,
                user_id=ctx.user_id,
                query=task,
            )
        if memory_block:
            system_prompt = f"{system_prompt}\n\n{memory_block}"

    history = [ChatMessage(role="system", content=system_prompt), ChatMessage(role="user", content=task)]
    started_at = time.monotonic()

    with tracer.start_as_current_span(
        "llm.delegate",
        attributes={
            "generation_id": ctx.generation_id,
            "assistant_id": str(spec.assistant_id),
            "tool_name": call.name,
        },
    ):
        try:
            async with asyncio.timeout(DELEGATION_TIMEOUT_SECONDS):
                inner = await _run_tool_loop(
                    ctx,
                    adapter=spec.adapter,
                    provider_model_id=spec.provider_model_id,
                    history=history,
                    tool_definitions=[to_tool_definition(t) for t in spec.tools],
                    tools_by_name={t.name: t for t in spec.tools},
                    delegates_by_name={},  # depth is always 1 — a delegate's own delegates never run
                    emit_deltas=False,
                    name_prefix=f"{call.name}/",
                )
        except TimeoutError:
            # The inner stream is cancelled along with it — its partial usage and any tool calls
            # it had already made are lost, a stated v1 limitation rather than a bug to chase.
            content = "Delegate timed out."
            await append_event(
                ctx.redis,
                ctx.generation_id,
                "tool_result",
                {"name": call.name, "ok": False, "content": content},
            )
            return _ToolInvocationRecord(
                tool_id=None,
                name=call.name,
                arguments=call.arguments,
                result=content,
                status=ToolInvocationStatus.ERROR,
                error=content,
                latency_ms=int((time.monotonic() - started_at) * 1000),
            )

    latency_ms = int((time.monotonic() - started_at) * 1000)
    usage = _DelegateUsage(
        model_id=spec.model_id,
        provider=spec.provider,
        tokens_in=inner.input_tokens or 0,
        tokens_out=inner.output_tokens or 0,
        latency_ms=latency_ms,
    )
    if inner.error_message:
        content = f"Delegate '{call.name}' failed: {inner.error_message}"
        status, error, billed_usage = ToolInvocationStatus.ERROR, content, None
    elif inner.stopped:
        content = "Delegation was stopped."
        status, error, billed_usage = ToolInvocationStatus.ERROR, content, usage
    else:
        content = inner.text
        if len(content) > MAX_RESULT_CHARS:
            content = content[:MAX_RESULT_CHARS] + "\n\n[...truncated]"
        status, error, billed_usage = ToolInvocationStatus.SUCCESS, None, usage

    await append_event(
        ctx.redis,
        ctx.generation_id,
        "tool_result",
        {"name": call.name, "ok": status == ToolInvocationStatus.SUCCESS, "content": content},
    )
    return _ToolInvocationRecord(
        tool_id=None,
        name=call.name,
        arguments=call.arguments,
        result=content,
        status=status,
        error=error,
        latency_ms=latency_ms,
        children=inner.invocations,
        usage=billed_usage,
    )


async def _execute_tool_call(
    ctx: _LoopContext,
    call: ToolCall,
    tools_by_name: dict[str, Tool],
    delegates_by_name: dict[str, DelegateSpec],
    name_prefix: str,
) -> _ToolInvocationRecord:
    """Run one requested call, emitting the SSE events either side of it, and return a record
    ready to persist once the generation finishes.

    A name found in `delegates_by_name` runs another assistant's own loop instead of a real tool
    — checked first, since delegate tool names are constructed (see delegate_tool_name) to never
    collide with a real one, so this check can never accidentally shadow an actual tool.
    """
    spec = delegates_by_name.get(call.name)
    if spec is not None:
        return await _run_delegation(ctx, call, spec)

    display_name = name_prefix + call.name
    await append_event(
        ctx.redis, ctx.generation_id, "tool_call", {"name": display_name, "arguments": call.arguments}
    )
    tool = tools_by_name.get(call.name)
    if tool is None:
        # The model asked for a tool that isn't (or is no longer) enabled — a stale definition
        # from earlier in a long conversation, not a reason to fail the whole generation.
        content = f"Tool '{call.name}' is not available."
        await append_event(
            ctx.redis,
            ctx.generation_id,
            "tool_result",
            {"name": display_name, "ok": False, "content": content},
        )
        return _ToolInvocationRecord(
            tool_id=None,
            name=display_name,
            arguments=call.arguments,
            result=content,
            status=ToolInvocationStatus.ERROR,
            error=content,
            latency_ms=0,
        )
    result, latency_ms = await execute_tool(tool, call.arguments)
    await append_event(
        ctx.redis,
        ctx.generation_id,
        "tool_result",
        {"name": display_name, "ok": result.ok, "content": result.content},
    )
    return _ToolInvocationRecord(
        tool_id=tool.id,
        name=display_name,
        arguments=call.arguments,
        result=result.content,
        status=ToolInvocationStatus.SUCCESS if result.ok else ToolInvocationStatus.ERROR,
        error=None if result.ok else result.content,
        latency_ms=latency_ms,
    )


async def _run_tool_loop(
    ctx: _LoopContext,
    *,
    adapter: LLMProvider,
    provider_model_id: str,
    history: list[ChatMessage],
    tool_definitions: list[ToolDefinition],
    tools_by_name: dict[str, Tool],
    delegates_by_name: dict[str, DelegateSpec],
    emit_deltas: bool,
    name_prefix: str = "",
) -> _LoopResult:
    """Drive the provider round-trip / tool-call cycle to a final answer, up to
    MAX_TOOL_ITERATIONS times. Used both for the outer generation's own turn (`emit_deltas=True`,
    `name_prefix=""`) and, one level deep, for a delegate's own turn (`emit_deltas=False` so its
    partial text never reaches the user directly as if it were the orchestrator talking;
    `name_prefix="ask_x/"` so any tool calls it makes are visibly nested under the delegation that
    triggered them). `delegates_by_name` is always empty on that inner call — depth is always 1.

    `history` is mutated in place as the loop appends the assistant's tool requests and their
    results, same as the original inline loop did.
    """
    text_parts: list[str] = []
    input_tokens: int | None = None
    output_tokens: int | None = None
    finish_reason = "stop"
    error_message: str | None = None
    stopped = False
    invocations: list[_ToolInvocationRecord] = []

    try:
        for _ in range(MAX_TOOL_ITERATIONS):
            # Checked at the top of every iteration too, not just between provider chunks — tool
            # (or delegate) execution itself can take a while, and a stop request shouldn't have
            # to wait for the *next* round trip to the provider to take effect.
            if await ctx.stop.check():
                finish_reason = "stopped"
                stopped = True
                break

            requested_calls: tuple[ToolCall, ...] = ()
            iteration_text: list[str] = []
            stream = adapter.stream(
                model=provider_model_id, messages=history, max_tokens=MAX_TOKENS, tools=tool_definitions
            )
            async for chunk in stream:
                if await ctx.stop.check():
                    finish_reason = "stopped"
                    stopped = True
                    break
                if isinstance(chunk, TextDelta):
                    iteration_text.append(chunk.text)
                    text_parts.append(chunk.text)
                    if emit_deltas:
                        await append_event(ctx.redis, ctx.generation_id, "delta", {"text": chunk.text})
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
                record = await _execute_tool_call(ctx, call, tools_by_name, delegates_by_name, name_prefix)
                invocations.append(record)
                history.append(
                    ChatMessage(role="tool", content=record.result, tool_call_id=call.id, tool_name=call.name)
                )
            # Loop again: the provider hasn't given a final answer yet, only asked for tools.
        else:
            # Exhausted every iteration without a break — the model never stopped calling tools
            # for a final answer. Doesn't override a real error or an explicit stop.
            if not error_message and not stopped:
                error_message = "The model kept calling tools without finishing an answer."
    except Exception as exc:  # noqa: BLE001 — any transport failure still needs a terminal result
        error_message = f"Streaming failed: {exc}"

    return _LoopResult(
        text="".join(text_parts),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        finish_reason=finish_reason,
        error_message=error_message,
        stopped=stopped,
        invocations=invocations,
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
    delegates: Sequence[DelegateSpec] = (),
) -> None:
    """Stream a reply from the provider, appending each chunk to Redis, then persist the result.

    When the model calls a tool (or a delegate, offered exactly like one — see DelegateSpec),
    this doesn't return — see _run_tool_loop, which this drives once for the outer turn.

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
    ctx = _LoopContext(
        redis=redis, generation_id=generation_id, stop=_StopSignal(pubsub),
        workspace_id=workspace_id, user_id=user_id,
    )

    tools_by_name = {t.name: t for t in tools}
    delegates_by_name = {d.tool_name: d for d in delegates}
    tool_definitions = [to_tool_definition(t) for t in tools] + [d.definition for d in delegates]

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
        result = await _run_tool_loop(
            ctx,
            adapter=adapter,
            provider_model_id=provider_model_id,
            history=history,
            tool_definitions=tool_definitions,
            tools_by_name=tools_by_name,
            delegates_by_name=delegates_by_name,
            emit_deltas=True,
        )
        span.set_attribute("finish_reason", result.finish_reason)
        span.set_attribute("tokens_in", result.input_tokens or 0)
        span.set_attribute("tokens_out", result.output_tokens or 0)
        span.set_attribute("tool_calls", len(result.invocations))
        if result.error_message:
            span.set_attribute("error", result.error_message)

    latency_ms = int((time.monotonic() - started_at) * 1000)

    async with async_session_factory() as db:
        # This connection never went through get_workspace_ctx (it's a background task, not a
        # request) — row-level security would otherwise block its own reads below.
        await set_workspace_scope(db, workspace_id)
        model = await db.get(LLMModel, model_id)
        own_cost_usd = None
        if model is not None and model.cost_per_mtok_in is not None and model.cost_per_mtok_out is not None:
            own_cost_usd = (
                (result.input_tokens or 0) / 1_000_000 * model.cost_per_mtok_in
                + (result.output_tokens or 0) / 1_000_000 * model.cost_per_mtok_out
            )
        assistant_message = Message(
            conversation_id=conversation_id,
            role=MessageRole.ASSISTANT,
            content=result.text,
            tokens_in=result.input_tokens,
            tokens_out=result.output_tokens,
            cost_usd=own_cost_usd,
            finish_reason=None if result.error_message else result.finish_reason,
            error=result.error_message,
        )
        db.add(assistant_message)
        conversation = await db.get(Conversation, conversation_id)
        if conversation is not None:
            conversation.updated_at = datetime.now(UTC)
        # Materializes assistant_message.id — needed below whether or not there's an error, since
        # a generation that failed on, say, its third round trip may still have two real tool
        # calls worth recording.
        await db.flush()

        # A delegate's model is very often not this generation's own — cached by id so a
        # generation with several delegate calls on the same model doesn't reload it each time.
        models_by_id: dict[uuid.UUID, LLMModel] = {model.id: model} if model is not None else {}
        total_cost_usd = own_cost_usd
        for record in result.invocations:
            for row in (record, *record.children):
                db.add(
                    ToolInvocation(
                        workspace_id=workspace_id,
                        message_id=assistant_message.id,
                        tool_id=row.tool_id,
                        name=row.name,
                        arguments=row.arguments,
                        result=row.result,
                        status=row.status,
                        error=row.error,
                        latency_ms=row.latency_ms,
                    )
                )
            if record.usage is None:
                continue
            usage = record.usage
            delegate_model = models_by_id.get(usage.model_id)
            if delegate_model is None:
                delegate_model = await db.get(LLMModel, usage.model_id)
                if delegate_model is not None:
                    models_by_id[usage.model_id] = delegate_model
            delegate_cost = 0.0
            if (
                delegate_model is not None
                and delegate_model.cost_per_mtok_in is not None
                and delegate_model.cost_per_mtok_out is not None
            ):
                delegate_cost = (
                    usage.tokens_in / 1_000_000 * delegate_model.cost_per_mtok_in
                    + usage.tokens_out / 1_000_000 * delegate_model.cost_per_mtok_out
                )
            total_cost_usd = (total_cost_usd or 0) + delegate_cost
            # A delegate's tokens were genuinely spent even if the outer turn later errored —
            # unlike the outer generation's own usage event below, this one is never skipped.
            await record_usage_event(
                db,
                workspace_id=workspace_id,
                user_id=user_id,
                conversation_id=conversation_id,
                message_id=assistant_message.id,
                model_id=usage.model_id,
                provider=usage.provider,
                tokens_in=usage.tokens_in,
                tokens_out=usage.tokens_out,
                cost_usd=delegate_cost,
                latency_ms=usage.latency_ms,
            )
        assistant_message.cost_usd = total_cost_usd

        if not result.error_message:
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
                tokens_in=result.input_tokens or 0,
                tokens_out=result.output_tokens or 0,
                cost_usd=own_cost_usd or 0,
                latency_ms=latency_ms,
            )
        await db.commit()

    if result.error_message:
        await append_event(redis, generation_id, "error", {"message": result.error_message})
    else:
        await append_event(redis, generation_id, "done", {"finish_reason": result.finish_reason})
    await clear_active_generation(redis, conversation_id)

    if not result.error_message and memory_assistant_id is not None:
        # Fire-and-forget, after the reply is already visible to its reader — a slow or
        # unreachable mem0 must never be the reason a reply takes longer to arrive. A failed
        # generation has nothing worth teaching mem0, hence the `not result.error_message` guard.
        memory_task = asyncio.create_task(
            record_turn(
                workspace_id=workspace_id,
                assistant_id=memory_assistant_id,
                user_id=user_id,
                user_message=user_content,
            )
        )
        _background_tasks.add(memory_task)
        memory_task.add_done_callback(_background_tasks.discard)

    await pubsub.unsubscribe(f"gen:{generation_id}:stop")
    await pubsub.aclose()  # type: ignore[no-untyped-call]  # redis-py's stubs omit this method's types
    await redis.aclose()
