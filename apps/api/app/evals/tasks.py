"""Golden task schema and loading — every malformed task fails here, naming itself, before any model call."""

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

Category = Literal["tool_selection", "multi_step", "delegation", "injection", "no_tool"]

DEFAULT_SYSTEM = "You are a helpful assistant. Use the tools you are given when they help answer the user."


class TaskLoadError(Exception):
    """A task file that can't be used — the message names the file, task id, and problem."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Matcher(_Strict):
    """How one tool argument is checked; exactly one of the matcher fields is set."""

    equals: Any = None
    contains: str | None = None
    regex: str | None = None
    approx: float | None = None
    tol: float | None = None
    any: Literal[True] | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> "Matcher":
        kinds = [k for k in ("contains", "regex", "approx", "any") if getattr(self, k) is not None]
        if "equals" in self.model_fields_set:
            kinds.append("equals")
        if len(kinds) != 1:
            raise ValueError(f"a matcher needs exactly one of equals/contains/regex/approx/any, got {kinds}")
        if self.approx is not None and self.tol is None:
            raise ValueError("approx needs a tol")
        if self.tol is not None and self.approx is None:
            raise ValueError("tol only applies to approx")
        return self


class ExpectedCall(_Strict):
    tool: str
    args: dict[str, Matcher] = Field(default_factory=dict)


class AnswerExpect(_Strict):
    must_contain: list[str] = Field(default_factory=list)
    must_not_contain: list[str] = Field(default_factory=list)
    judge: str | None = None


class Expect(_Strict):
    calls: list[ExpectedCall] = Field(default_factory=list)
    order: Literal["any", "strict", "subsequence"] = "any"
    forbidden_tools: list[str] = Field(default_factory=list)
    max_calls: int = Field(ge=0)
    answer: AnswerExpect = Field(default_factory=AnswerExpect)


class DelegateDef(_Strict):
    name: str
    instructions: str
    tools: list[str] = Field(default_factory=list)


class ToolDef(_Strict):
    name: str
    description: str
    parameters: dict[str, Any]


class Task(_Strict):
    id: str
    category: Category
    prompt: str
    system: str = DEFAULT_SYSTEM
    tools: list[str]
    fixtures: dict[str, str] = Field(default_factory=dict)
    delegates: list[DelegateDef] = Field(default_factory=list)
    expect: Expect


def load_tool_catalog(path: Path) -> dict[str, ToolDef]:
    """Read the shared tool definitions every task references by name."""
    raw = yaml.safe_load(path.read_text()) or []
    try:
        tools = [ToolDef.model_validate(item) for item in raw]
    except ValidationError as exc:
        raise TaskLoadError(f"{path.name}: {exc}") from exc
    return {tool.name: tool for tool in tools}


def load_tasks(tasks_dir: Path, catalog: dict[str, ToolDef]) -> list[Task]:
    """Load every *.yaml task file in name order, rejecting bad fields, duplicates, and unknown tools."""
    tasks: list[Task] = []
    seen: set[str] = set()
    for path in sorted(tasks_dir.glob("*.yaml")):
        for item in yaml.safe_load(path.read_text()) or []:
            task_id = item.get("id", "<no id>") if isinstance(item, dict) else "<not a mapping>"
            try:
                task = Task.model_validate(item)
            except ValidationError as exc:
                raise TaskLoadError(f"{path.name}: task {task_id}: {exc}") from exc
            if task.id in seen:
                raise TaskLoadError(f"{path.name}: duplicate task id {task.id}")
            seen.add(task.id)
            wanted = set(task.tools) | {t for d in task.delegates for t in d.tools}
            unknown = sorted(wanted - catalog.keys())
            if unknown:
                raise TaskLoadError(f"{path.name}: task {task.id}: unknown tools {unknown}")
            tasks.append(task)
    return tasks
