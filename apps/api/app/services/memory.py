"""The memory layer (ReMind): curated facts an assistant's creator teaches it, and personal
memories mem0 learns about each user from their own chats with it — kept in two structurally
separate mem0 namespaces so a bug in a filter can drop a memory, never leak one.

mem0 is the store; nothing here mirrors a memory locally in Postgres. Its own `add` is
asynchronous (it returns a queued event, not a memory id) and it consolidates and rewrites
memories on its own, so a local copy would start drifting the moment it was written. What this
module keeps in Postgres instead is just the encrypted API key and the `memory_enabled` bit on
each assistant; authorization comes from the scope encoding below plus a verify-before-delete
step, and the audit trail is the same `audit_logs` table every other privileged action uses.
"""

import asyncio
import uuid
from typing import Literal

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import EncryptedSecret, decrypt_secret, encrypt_secret
from app.core.db import async_session_factory, set_workspace_scope
from app.core.errors import InsufficientRole, MemoryNotConfigured, MemoryNotFound, MemoryUpstreamError
from app.core.logging import get_logger
from app.core.redis import new_redis_client
from app.memory import mem0
from app.models import Assistant, MemoryCredential, User
from app.services.assistants import get_assistant

logger = get_logger(__name__)

Scope = Literal["curated", "personal"]

# What a chat-time search asks for, and how much of it is worth reading — a handful of the most
# relevant facts, not a wall of everything mem0 has ever learned.
#
# A wider candidate pool (top_k) than what's actually injected: mem0 scores and orders the whole
# pool before threshold trims it, so a too-small top_k can drop the actually-relevant memory
# before it's ever considered, not just before it's shown. `threshold` is mem0's own documented
# default (0.1) rather than a stricter one this project picked itself — a real personal-memory
# corpus is small, and a memory that's genuinely the best (or only) match for a query can still
# score well below what a dense, large corpus's matches typically would.
_SEARCH_TOP_K = 20
_SEARCH_THRESHOLD = 0.1

# Biases mem0's own extraction classifier for record_turn()'s writes — observed live to be needed:
# a user explicitly saying "I like song X, remember that", with the assistant confirming it back,
# still produced zero extracted memories without this. `agent_custom_instructions`, specifically
# — not the more commonly-referenced `custom_instructions` — is what actually governs extraction
# here, per mem0's own docs: once both `agent_id` and `user_id` are on one call (mem0's "hybrid
# mode", which every personal-scope write is), `custom_instructions` covers only *non-assistant*
# memories, and would silently have had no effect on these calls at all.
_PERSONAL_MEMORY_INCLUDES = (
    "personal preferences, likes and dislikes, opinions, and anything the user explicitly asks "
    "to be remembered"
)
_PERSONAL_MEMORY_INSTRUCTIONS = (
    "Capture any preference, like, dislike, or opinion the user states about themselves, even a "
    "small one (a favorite song, food, color, and similar), and anything the user explicitly "
    "asks you to remember. Do not skip these just because they seem minor."
)
_MEMORY_BLOCK_MAX_CHARS = 2_000

# record_turn()'s `add()` reports success once mem0 *accepts* a memory for extraction, not once
# it's actually searchable — observed live to lag real search calls by well over a minute for a
# personal-scope write. Retrying every empty search on a fixed delay would be the wrong trade:
# most turns have no relevant memory at all, and that (common, legitimate) case shouldn't pay
# extra latency for a problem it doesn't have. Instead, record_turn marks in Redis, with a TTL,
# that *this exact (workspace, assistant, user)* has a write that may still be indexing —
# retrieve_for_turn only pays the retry's cost when that marker says it's actually warranted.
_PENDING_REINDEX_TTL_SECONDS = 90
_REINDEX_RETRY_DELAY_SECONDS = 3.0


def _pending_reindex_key(workspace_id: uuid.UUID, assistant_id: uuid.UUID, user_id: uuid.UUID) -> str:
    return f"memory:pending:{workspace_id}:{assistant_id}:{user_id}"


# ---------- Scope encoding — the only place these strings are built ----------


def curated_agent_id(workspace_id: uuid.UUID, assistant_id: uuid.UUID) -> str:
    """The mem0 namespace for one assistant's creator-curated facts — shared by everyone who
    chats with it, never written to by chatting itself (see record_turn)."""
    return f"ws:{workspace_id}:assistant:{assistant_id}:curated"


def personal_agent_id(workspace_id: uuid.UUID, assistant_id: uuid.UUID) -> str:
    """The mem0 namespace for what this assistant has learned about individual users — always
    paired with a `user_id` (see user_entity_id) so one user's chats can never surface in
    another's. A *different* agent_id than curated_agent_id's, not the same one distinguished by
    the presence of user_id — sharing one namespace would make any query that filters on
    agent_id alone return every user's personal memories at once."""
    return f"ws:{workspace_id}:assistant:{assistant_id}:user"


def user_entity_id(workspace_id: uuid.UUID, user_id: uuid.UUID) -> str:
    """The mem0 `user_id` value for one member of one workspace."""
    return f"ws:{workspace_id}:user:{user_id}"


# ---------- Credential ----------


async def get_credential_row(db: AsyncSession, *, workspace_id: uuid.UUID) -> MemoryCredential | None:
    """Load the workspace's mem0 credential row, or None if it hasn't set one yet."""
    return await db.get(MemoryCredential, workspace_id)


async def has_credential(db: AsyncSession, *, workspace_id: uuid.UUID) -> bool:
    """Whether the workspace has a mem0 key configured — what the settings page checks before
    showing "set a key" versus "rotate it"."""
    return await get_credential_row(db, workspace_id=workspace_id) is not None


async def set_credential(
    db: AsyncSession, *, workspace_id: uuid.UUID, created_by: User, api_key: str
) -> MemoryCredential:
    """Set or rotate the workspace's mem0 key — the same create-or-rotate-in-place shape
    enable_web_search() already uses for a workspace's Tavily key."""
    secret = encrypt_secret(api_key)
    existing = await get_credential_row(db, workspace_id=workspace_id)
    if existing is not None:
        existing.ciphertext = secret.ciphertext
        existing.nonce = secret.nonce
        existing.wrapped_key = secret.wrapped_key
        await db.flush()
        return existing
    credential = MemoryCredential(
        workspace_id=workspace_id,
        ciphertext=secret.ciphertext,
        nonce=secret.nonce,
        wrapped_key=secret.wrapped_key,
        created_by=created_by.id,
    )
    db.add(credential)
    await db.flush()
    return credential


async def delete_credential(db: AsyncSession, *, workspace_id: uuid.UUID) -> None:
    """Remove the workspace's mem0 key. Memories already stored in mem0 aren't deleted by this —
    it only stops this workspace's chats and settings pages from reaching them."""
    credential = await get_credential_row(db, workspace_id=workspace_id)
    if credential is not None:
        await db.delete(credential)
        await db.flush()


async def _require_api_key(db: AsyncSession, *, workspace_id: uuid.UUID) -> str:
    """The decrypted mem0 key, or raise if the workspace hasn't set one — used by every route
    that talks to mem0 on demand (listing, adding, deleting). Chat-time retrieval and recording
    call get_credential_row directly instead, since a missing key there should mean "no memories
    this turn," not an error that fails the generation.
    """
    credential = await get_credential_row(db, workspace_id=workspace_id)
    if credential is None:
        raise MemoryNotConfigured()
    secret = EncryptedSecret(
        ciphertext=credential.ciphertext, nonce=credential.nonce, wrapped_key=credential.wrapped_key
    )
    return decrypt_secret(secret)


def _assert_can_manage_curated(assistant: Assistant, *, caller_id: uuid.UUID, is_owner: bool) -> None:
    """Only the assistant's own creator, or the workspace's owner, may add or delete a curated
    memory — the fine-tuning half of ReMind, as opposed to what chatting with it accumulates."""
    if assistant.created_by != caller_id and not is_owner:
        raise InsufficientRole()


# ---------- Curated memories ----------


async def list_curated(
    db: AsyncSession, *, workspace_id: uuid.UUID, assistant_id: uuid.UUID
) -> list[dict[str, object]]:
    """List an assistant's curated memories — visible to any member who can use the assistant,
    not just its creator or the workspace owner (only adding and deleting are restricted)."""
    await get_assistant(db, workspace_id=workspace_id, assistant_id=assistant_id)  # 404s if not ours
    api_key = await _require_api_key(db, workspace_id=workspace_id)
    ns = curated_agent_id(workspace_id, assistant_id)
    results = await mem0.list_memories(api_key=api_key, filters={"agent_id": ns})
    if results is None:
        raise MemoryUpstreamError()
    return results


async def add_curated(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    assistant_id: uuid.UUID,
    caller_id: uuid.UUID,
    is_owner: bool,
    text: str,
) -> None:
    """Teach an assistant a fact directly — stored verbatim (`infer=False`, no interpretation)
    and excluded from mem0's own later consolidation (`immutable=True`), so nothing a user says
    in an ordinary chat can ever quietly rewrite it."""
    assistant = await get_assistant(db, workspace_id=workspace_id, assistant_id=assistant_id)
    _assert_can_manage_curated(assistant, caller_id=caller_id, is_owner=is_owner)
    api_key = await _require_api_key(db, workspace_id=workspace_id)
    ns = curated_agent_id(workspace_id, assistant_id)
    ok = await mem0.add(
        api_key=api_key,
        messages=[{"role": "user", "content": text}],
        agent_id=ns,
        infer=False,
        immutable=True,
    )
    if not ok:
        raise MemoryUpstreamError()


# ---------- Personal memories ----------


async def list_personal(
    db: AsyncSession, *, workspace_id: uuid.UUID, assistant_id: uuid.UUID, user_id: uuid.UUID
) -> list[dict[str, object]]:
    """List *this user's own* memories with one assistant — never anyone else's; there is no
    "list every user's personal memories" function anywhere in this module, on purpose."""
    await get_assistant(db, workspace_id=workspace_id, assistant_id=assistant_id)
    api_key = await _require_api_key(db, workspace_id=workspace_id)
    ns = personal_agent_id(workspace_id, assistant_id)
    entity = user_entity_id(workspace_id, user_id)
    results = await mem0.list_memories(
        api_key=api_key, filters={"AND": [{"agent_id": ns}, {"user_id": entity}]}
    )
    if results is None:
        raise MemoryUpstreamError()
    return results


# ---------- Deleting a memory, safely ----------


async def delete_memory(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    assistant_id: uuid.UUID,
    memory_id: str,
    scope: Scope,
    caller_id: uuid.UUID,
    is_owner: bool,
) -> None:
    """Delete one memory — but only after confirming it's actually *in the scope the caller is
    authorized for*, not just that some memory with this id exists somewhere in mem0. That's what
    makes "user B deletes user A's memory by guessing its id" impossible even if the
    authorization check above it had a bug: the id has to turn up in a listing B is genuinely
    allowed to see before the delete call is ever made.
    """
    assistant = await get_assistant(db, workspace_id=workspace_id, assistant_id=assistant_id)
    api_key = await _require_api_key(db, workspace_id=workspace_id)

    if scope == "curated":
        _assert_can_manage_curated(assistant, caller_id=caller_id, is_owner=is_owner)
        filters: dict[str, object] = {"agent_id": curated_agent_id(workspace_id, assistant_id)}
    else:
        entity = user_entity_id(workspace_id, caller_id)
        filters = {
            "AND": [
                {"agent_id": personal_agent_id(workspace_id, assistant_id)},
                {"user_id": entity},
            ]
        }

    existing = await mem0.list_memories(api_key=api_key, filters=filters)
    if existing is None:
        # Couldn't even check — genuinely different from "checked, and it wasn't there" (below),
        # which is why this isn't folded into the same 404 as a real not-found.
        raise MemoryUpstreamError()
    if not any(str(m.get("id")) == memory_id for m in existing):
        raise MemoryNotFound()

    if not await mem0.delete(api_key=api_key, memory_id=memory_id):
        raise MemoryUpstreamError()


# ---------- The chat-pipeline hooks ----------


async def _search_and_filter(
    api_key: str, *, query: str, filters: dict[str, object], curated_ns: str, personal_ns: str, entity: str
) -> list[dict[str, object]]:
    """One search call, then post-filtered against the two scopes this exact caller is entitled
    to — independent of whether mem0's own filter is applied correctly. A row missing or
    mismatching `agent_id` (or, for the personal scope, `user_id`) is dropped rather than
    trusted, so a filtering bug on mem0's side can only ever hide a memory, never leak one that
    isn't this caller's to see."""
    results = await mem0.search(
        api_key=api_key,
        query=query,
        filters=filters,
        top_k=_SEARCH_TOP_K,
        threshold=_SEARCH_THRESHOLD,
        rerank=True,
    )
    if not results:
        return []
    return [
        r
        for r in results
        if r.get("agent_id") == curated_ns
        or (r.get("agent_id") == personal_ns and r.get("user_id") == entity)
    ]


async def retrieve_for_turn(
    db: AsyncSession,
    redis: Redis,
    *,
    workspace_id: uuid.UUID,
    assistant_id: uuid.UUID,
    user_id: uuid.UUID,
    query: str,
) -> str | None:
    """Search the two scopes a chat with this assistant may ever draw on — its curated facts, and
    this user's own personal memories with it — and return them as a labeled block to fold into
    the system prompt. Never raises and never fails a generation: a missing key or a mem0 outage
    just means no memories reach this turn.
    """
    credential = await get_credential_row(db, workspace_id=workspace_id)
    if credential is None:
        return None
    secret = EncryptedSecret(
        ciphertext=credential.ciphertext, nonce=credential.nonce, wrapped_key=credential.wrapped_key
    )
    api_key = decrypt_secret(secret)

    curated_ns = curated_agent_id(workspace_id, assistant_id)
    personal_ns = personal_agent_id(workspace_id, assistant_id)
    entity = user_entity_id(workspace_id, user_id)
    filters: dict[str, object] = {
        "OR": [
            {"agent_id": curated_ns},
            {"AND": [{"agent_id": personal_ns}, {"user_id": entity}]},
        ]
    }

    kept = await _search_and_filter(
        api_key, query=query, filters=filters, curated_ns=curated_ns, personal_ns=personal_ns, entity=entity
    )
    if not kept and await redis.get(_pending_reindex_key(workspace_id, assistant_id, user_id)):
        # A write for this exact (workspace, assistant, user) was queued recently enough that it
        # may not be searchable yet (see record_turn) — worth one bounded retry rather than
        # silently treating "not indexed yet" the same as "nothing relevant exists." Every other
        # empty search (the common case — most turns have no relevant memory at all) skips this
        # entirely and pays no extra latency for a problem it doesn't have.
        await asyncio.sleep(_REINDEX_RETRY_DELAY_SECONDS)
        kept = await _search_and_filter(
            api_key,
            query=query,
            filters=filters,
            curated_ns=curated_ns,
            personal_ns=personal_ns,
            entity=entity,
        )
    if not kept:
        return None

    lines = [f"- {r.get('memory', '')}" for r in kept if r.get("memory")]
    if not lines:
        return None
    block = "Relevant memory:\n" + "\n".join(lines)
    if len(block) > _MEMORY_BLOCK_MAX_CHARS:
        block = block[:_MEMORY_BLOCK_MAX_CHARS] + "\n\n[...truncated]"
    return block


async def record_turn(
    *,
    workspace_id: uuid.UUID,
    assistant_id: uuid.UUID,
    user_id: uuid.UUID,
    user_message: str,
    assistant_message: str,
) -> None:
    """Let mem0 learn from one completed turn — always into the *personal* scope. There is no
    parameter here capable of targeting the curated scope: "chatting fine-tunes the assistant"
    isn't a bug that can be introduced later by a careless call site, it's unexpressible by this
    function's own signature.

    Runs fire-and-forget from _run_generation, after that generation's own reply has already been
    committed and its terminal SSE event sent — so a slow or unreachable mem0 can never delay a
    reply reaching its reader. Opens its own database session and Redis client for exactly that
    reason: whichever ones the generation used are already closed by the time this runs.
    """
    async with async_session_factory() as db:
        await set_workspace_scope(db, workspace_id)
        credential = await get_credential_row(db, workspace_id=workspace_id)
        if credential is None:
            return
        secret = EncryptedSecret(
            ciphertext=credential.ciphertext, nonce=credential.nonce, wrapped_key=credential.wrapped_key
        )
        api_key = decrypt_secret(secret)

    ok = await mem0.add(
        api_key=api_key,
        messages=[
            {"role": "user", "content": user_message},
            {"role": "assistant", "content": assistant_message},
        ],
        agent_id=personal_agent_id(workspace_id, assistant_id),
        user_id=user_entity_id(workspace_id, user_id),
        infer=True,
        includes=_PERSONAL_MEMORY_INCLUDES,
        agent_custom_instructions=_PERSONAL_MEMORY_INSTRUCTIONS,
    )
    if not ok:
        logger.warning(
            "memory.record_turn_failed", workspace_id=str(workspace_id), assistant_id=str(assistant_id)
        )
        return

    # Marks that a search against this exact scope, soon, may need retrieve_for_turn's one retry
    # — mem0 accepted this write, but that isn't the same moment as it becoming searchable.
    redis = new_redis_client()
    try:
        await redis.set(
            _pending_reindex_key(workspace_id, assistant_id, user_id),
            "1",
            ex=_PENDING_REINDEX_TTL_SECONDS,
        )
    finally:
        await redis.aclose()
