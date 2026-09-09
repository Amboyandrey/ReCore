"""Runs one tool call against its Tool row, whatever kind it is, under a hard timeout and a
result-size cap — nothing dispatched here can hang a generation or flood the model's context with
an oversized result."""

import asyncio
import time

from app.models import Tool, ToolKind
from app.tools.base import ToolExecutionResult
from app.tools.registry import BUILTIN_TOOLS

MAX_RESULT_CHARS = 8_000
EXECUTION_TIMEOUT_SECONDS = 15.0


async def execute_tool(tool: Tool, arguments: dict[str, object]) -> tuple[ToolExecutionResult, int]:
    """Execute one call, returning its (possibly truncated) result and how long it took, in ms.

    Never raises: a tool that times out, throws, or returns something huge all turn into a
    normal-shaped ToolExecutionResult the caller feeds back to the model — the same "never fail
    the whole thing over one bad input" contract attachment extraction already follows.
    """
    started = time.monotonic()
    try:
        async with asyncio.timeout(EXECUTION_TIMEOUT_SECONDS):
            result = await _dispatch(tool, arguments)
    except TimeoutError:
        result = ToolExecutionResult(ok=False, content="The tool timed out.")
    except Exception as exc:  # noqa: BLE001 — a broken tool must not take the generation down
        result = ToolExecutionResult(ok=False, content=f"The tool failed: {exc}")
    latency_ms = int((time.monotonic() - started) * 1000)

    if len(result.content) > MAX_RESULT_CHARS:
        result = ToolExecutionResult(
            ok=result.ok, content=result.content[:MAX_RESULT_CHARS] + "\n\n[...truncated]"
        )
    return result, latency_ms


async def _dispatch(tool: Tool, arguments: dict[str, object]) -> ToolExecutionResult:
    """Route to the right executor for this tool's kind."""
    if tool.kind == ToolKind.BUILTIN:
        spec = BUILTIN_TOOLS.get(tool.name)
        if spec is None:
            return ToolExecutionResult(ok=False, content=f"Unknown built-in tool: {tool.name}")
        return await spec.execute(tool, arguments)
    # HTTP tools are a later phase — see docs/ARCHITECTURE.md.
    return ToolExecutionResult(ok=False, content="This tool type isn't supported yet.")
