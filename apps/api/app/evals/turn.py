"""Builds the in-memory AssistantTurn one task runs on — transient rows only, no database writes."""

import uuid

from app.evals.fixtures import url_for
from app.evals.models_config import ModelSpec
from app.evals.tasks import Task, ToolDef
from app.models import LLMModel, Tool, ToolKind
from app.providers.base import LLMProvider
from app.services.assistant_runtime import (
    AssistantTurn,
    DelegateSpec,
    delegate_definition,
    delegate_tool_name,
)


def _tool(task: Task, definition: ToolDef) -> Tool:
    fixture_set = task.fixtures.get(definition.name, "default")
    return Tool(
        id=uuid.uuid4(),
        name=definition.name,
        description=definition.description,
        parameters=definition.parameters,
        kind=ToolKind.HTTP,
        method="POST",
        url=url_for(fixture_set, definition.name),
    )


def build_turn(
    task: Task, catalog: dict[str, ToolDef], spec: ModelSpec, adapter: LLMProvider
) -> AssistantTurn:
    """Offer the task's tools and delegates exactly as chat would, all on this one model and adapter."""
    model = LLMModel(
        id=uuid.uuid4(),
        provider_model_id=spec.provider_model_id,
        display_name=spec.id,
        cost_per_mtok_in=spec.cost_per_mtok_in,
        cost_per_mtok_out=spec.cost_per_mtok_out,
    )
    taken = set(task.tools)
    delegates: list[DelegateSpec] = []
    for d in task.delegates:
        tool_name = delegate_tool_name(d.name, taken)
        taken.add(tool_name)
        delegates.append(
            DelegateSpec(
                assistant_id=uuid.uuid4(),
                tool_name=tool_name,
                definition=delegate_definition(tool_name, d.name),
                instructions=d.instructions,
                adapter=adapter,
                provider=spec.provider,
                model_id=model.id,
                provider_model_id=spec.provider_model_id,
                tools=[_tool(task, catalog[name]) for name in d.tools],
                memory_active=False,
            )
        )
    return AssistantTurn(
        system_prompt=task.system,
        adapter=adapter,
        provider=spec.provider,
        model=model,
        tools=[_tool(task, catalog[name]) for name in task.tools],
        delegates=delegates,
        sources=[],
        memory_assistant_id=None,
    )
