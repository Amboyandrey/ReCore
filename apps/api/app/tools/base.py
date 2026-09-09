"""What every tool executor produces — the shared shape execute.py and the agent loop both use,
regardless of which kind of tool actually ran."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolExecutionResult:
    """The outcome of running one tool call.

    `content` is fed back to the model as the tool's result either way — an error is still a
    result the model gets to see and react to (try a different query, apologize, ask the user),
    not a reason to fail the whole generation.
    """

    ok: bool
    content: str
