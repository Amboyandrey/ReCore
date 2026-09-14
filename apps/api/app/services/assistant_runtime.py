"""Resolving what one assistant needs to actually run a turn — its system prompt (instructions
plus any recalled memory or retrieved knowledge folded in), its offered tools, and the other
assistants it may delegate to. Used by both chat.py's send_message (one turn inside a
conversation) and workers/run_workflow.py (one step of a background workflow, no conversation at
all) — the two are otherwise unrelated call sites that would each duplicate this resolution.
"""

import re
import uuid
from dataclasses import dataclass

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Assistant, LLMModel, Provider, ProviderCredential, Tool
from app.providers.base import LLMProvider, ToolDefinition
from app.providers.registry import build_provider
from app.services.assistants import list_assistant_delegates, list_assistant_tools
from app.services.credentials import decrypt_credential_key, get_credential
from app.services.flags import evaluate_flag
from app.services.knowledge import SourceHit
from app.services.knowledge import retrieve_for_turn as retrieve_knowledge_for_turn
from app.services.memory import retrieve_for_turn as retrieve_memory_for_turn

_DELEGATE_NAME_SANITIZER = re.compile(r"[^a-zA-Z0-9_-]+")


def delegate_tool_name(name: str, taken: set[str]) -> str:
    """Turn an assistant's display name into a legal, collision-free tool name to offer the
    model — assistant names aren't unique per workspace, but a tool name must be (and must match
    ^[a-zA-Z0-9_-]+$, capped at 64 chars, same as a real tool's — see schemas/tool.py).

    `taken` starts as the orchestrator's own real tool names and grows by one with every delegate
    named — the caller does that, not this function — which is what guarantees an `ask_*` name
    can never collide with a real tool, letting chat.py's _execute_tool_call check delegates
    first safely.
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


@dataclass(frozen=True)
class DelegateSpec:
    """One assistant this turn's assistant may hand a task to — everything needed to run its own
    tool-calling loop, resolved once per turn (see build_delegate_specs) so the loop itself never
    has to touch the database. `tool_name` is what the model actually sees and calls; `definition`
    is the synthesized ToolDefinition offered alongside the real tools."""

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


async def build_delegate_specs(
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
    its own model/credential/adapter if it has one (falling back to the caller's default), its
    own tools, and whether memory recall applies to it.

    A delegate whose own model's provider has been disabled is skipped outright rather than
    silently run on a different model its author never chose — the same posture ProviderDisabled
    already takes for the outer turn's own model. Depth is always 1: this never recurses into a
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


@dataclass(frozen=True)
class AssistantTurn:
    """Everything one turn needs to actually run: the composed system prompt (instructions plus
    any recalled memory or retrieved knowledge), the provider adapter to call, the offered tools
    and delegates, the knowledge sources folded in (for the caller to surface/persist), and — when
    memory should be taught from this turn afterward — which assistant's personal scope to teach
    it into. `adapter`/`provider`/`model` are simply echoed back from the caller's own inputs, so
    a caller holding one AssistantTurn has everything run_assistant_task needs without threading
    three more parameters alongside it.
    """

    system_prompt: str
    adapter: LLMProvider
    provider: Provider
    model: LLMModel
    tools: list[Tool]
    delegates: list[DelegateSpec]
    sources: list[SourceHit]
    memory_assistant_id: uuid.UUID | None


async def prepare_assistant_turn(
    db: AsyncSession,
    redis: Redis,
    *,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    assistant: Assistant,
    model: LLMModel,
    credential: ProviderCredential,
    adapter: LLMProvider,
    query: str,
) -> AssistantTurn:
    """Resolve everything `assistant` needs for one turn answering `query` — memory recall,
    knowledge retrieval, its assigned tools, and any assistants it may delegate to — each gated on
    its own feature flag exactly as chat.py's send_message always has, so an assistant behaves
    identically whether it's answering in a live conversation or running one workflow step.

    `model`/`credential`/`adapter` are the ones this turn will actually run on — the conversation's
    own model in chat, or the step's resolved model in a workflow (see services/workflows.py's
    validation, which guarantees every step always has one). They're also what a delegate with no
    model of its own falls back to (see build_delegate_specs).
    """
    system_prompt = assistant.instructions

    memory_flag_enabled = await evaluate_flag(
        db, redis, key="memory", workspace_id=workspace_id, user_id=user_id
    )
    memory_active = memory_flag_enabled and assistant.memory_enabled
    memory_assistant_id: uuid.UUID | None = None
    if memory_active:
        memory_assistant_id = assistant.id
        memory_block = await retrieve_memory_for_turn(
            db, workspace_id=workspace_id, assistant_id=assistant.id, user_id=user_id, query=query
        )
        if memory_block:
            system_prompt = f"{system_prompt}\n\n{memory_block}" if system_prompt else memory_block

    sources: list[SourceHit] = []
    knowledge_flag_enabled = await evaluate_flag(
        db, redis, key="knowledge", workspace_id=workspace_id, user_id=user_id
    )
    if knowledge_flag_enabled:
        knowledge_result = await retrieve_knowledge_for_turn(
            db, workspace_id=workspace_id, assistant_id=assistant.id, query=query
        )
        if knowledge_result:
            system_prompt = (
                f"{system_prompt}\n\n{knowledge_result.block}" if system_prompt else knowledge_result.block
            )
            sources = knowledge_result.sources

    tools_enabled = await evaluate_flag(
        db, redis, key="tools", workspace_id=workspace_id, user_id=user_id
    )
    tools = await list_assistant_tools(db, assistant_id=assistant.id) if tools_enabled else []

    delegates: list[DelegateSpec] = []
    delegation_enabled = await evaluate_flag(
        db, redis, key="delegation", workspace_id=workspace_id, user_id=user_id
    )
    if delegation_enabled:
        delegates = await build_delegate_specs(
            db,
            redis,
            workspace_id=workspace_id,
            user_id=user_id,
            orchestrator=assistant,
            default_adapter=adapter,
            default_provider=credential.provider,
            default_model=model,
            tools_enabled=tools_enabled,
            memory_flag_enabled=memory_flag_enabled,
            reserved_names={t.name for t in tools},
        )

    return AssistantTurn(
        system_prompt=system_prompt,
        adapter=adapter,
        provider=credential.provider,
        model=model,
        tools=tools,
        delegates=delegates,
        sources=sources,
        memory_assistant_id=memory_assistant_id,
    )
