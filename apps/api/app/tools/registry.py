"""Maps a built-in tool's name to its definition and executor — the one place that knows about
every built-in tool this app ships. Registering a new one means adding an entry here, plus a
matching row (see services/tools.py's create-or-enable helpers) — the same "table row is the
source of truth" shape the flags migration uses for provider killswitches."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.models import Tool
from app.tools import web_search
from app.tools.base import ToolExecutionResult

_Executor = Callable[[Tool, dict[str, object]], Awaitable[ToolExecutionResult]]


@dataclass(frozen=True)
class BuiltinToolSpec:
    """A built-in tool's fixed definition — what a Tool row of kind BUILTIN is filled in from."""

    name: str
    description: str
    parameters: dict[str, object]
    execute: _Executor


BUILTIN_TOOLS: dict[str, BuiltinToolSpec] = {
    web_search.NAME: BuiltinToolSpec(
        name=web_search.NAME,
        description=web_search.DESCRIPTION,
        parameters=web_search.PARAMETERS,
        execute=web_search.execute,
    ),
}
