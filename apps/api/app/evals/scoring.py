"""Hard scoring — pure checks over one loop result; odd model output fails a check, never raises."""

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from app.evals.tasks import ExpectedCall, Matcher, Task
from app.models.tool_invocation_status import ToolInvocationStatus
from app.services.chat import TOOL_LOOP_EXHAUSTED, LoopResult, ToolInvocationRecord


@dataclass(frozen=True)
class ObservedCall:
    """One tool call the model made, delegate children included under their `ask_x/` prefix."""

    name: str
    arguments: dict[str, object]
    ok: bool


@dataclass
class CheckResult:
    passed: bool
    checks: dict[str, bool]
    reasons: list[str] = field(default_factory=list)


def flatten_calls(invocations: list[ToolInvocationRecord]) -> list[ObservedCall]:
    """Depth-first: each call, then the calls its delegate made, in the order they happened."""
    flat: list[ObservedCall] = []
    for record in invocations:
        arguments = record.arguments if isinstance(record.arguments, dict) else {}
        flat.append(ObservedCall(record.name, arguments, record.status == ToolInvocationStatus.SUCCESS))
        flat.extend(flatten_calls(record.children))
    return flat


def match_value(m: Matcher, value: object) -> bool:
    """Check one argument value against its matcher."""
    if m.any:
        return True
    if m.contains is not None:
        return m.contains.lower() in str(value).lower()
    if m.regex is not None:
        return re.search(m.regex, str(value)) is not None
    if m.approx is not None and m.tol is not None:
        if isinstance(value, bool) or not isinstance(value, int | float | str):
            return False
        try:
            return abs(float(value) - m.approx) <= m.tol
        except ValueError:
            return False
    return bool(type(value) is type(m.equals) and value == m.equals)


def _call_matches(expected: ExpectedCall, observed: ObservedCall) -> bool:
    if expected.tool != observed.name:
        return False
    return all(
        name in observed.arguments and match_value(matcher, observed.arguments[name])
        for name, matcher in expected.args.items()
    )


def _match_any(expected: list[ExpectedCall], observed: list[ObservedCall]) -> bool:
    """One-to-one assignment of expected to observed calls, by backtracking over short lists."""
    used = [False] * len(observed)

    def assign(i: int) -> bool:
        if i == len(expected):
            return True
        for j, call in enumerate(observed):
            if not used[j] and _call_matches(expected[i], call):
                used[j] = True
                if assign(i + 1):
                    return True
                used[j] = False
        return False

    return assign(0)


def _match_subsequence(expected: list[ExpectedCall], observed: list[ObservedCall]) -> bool:
    remaining = iter(observed)
    return all(any(_call_matches(e, o) for o in remaining) for e in expected)


def _match_strict(expected: list[ExpectedCall], observed: list[ObservedCall]) -> bool:
    return len(expected) == len(observed) and all(map(_call_matches, expected, observed))


_ORDER_MATCHERS: dict[str, Callable[[list[ExpectedCall], list[ObservedCall]], bool]] = {
    "any": _match_any,
    "subsequence": _match_subsequence,
    "strict": _match_strict,
}


def score_hard(task: Task, result: LoopResult) -> CheckResult:
    """Run every rule-based check a task defines; the task passes only if all of them do."""
    expect = task.expect
    calls = flatten_calls(result.invocations)
    names = [c.name for c in calls]
    reasons: list[str] = []

    missing = sorted({e.tool for e in expect.calls} - set(names))
    forbidden = sorted(set(expect.forbidden_tools) & set(names))
    tool_selection = not missing and not forbidden
    if missing:
        reasons.append(f"expected tools never called: {missing}")
    if forbidden:
        reasons.append(f"forbidden tools called: {forbidden}")

    arguments = _ORDER_MATCHERS[expect.order](expect.calls, calls)
    if not arguments:
        reasons.append(f"calls did not match expected ({expect.order} order): got {names}")

    trajectory = len(calls) <= expect.max_calls and result.error_message != TOOL_LOOP_EXHAUSTED
    if len(calls) > expect.max_calls:
        reasons.append(f"{len(calls)} calls exceeds max_calls {expect.max_calls}")
    if result.error_message == TOOL_LOOP_EXHAUSTED:
        reasons.append("hit the tool iteration limit without a final answer")

    answer = result.text.lower()
    absent = [s for s in expect.answer.must_contain if s.lower() not in answer]
    present = [s for s in expect.answer.must_not_contain if s.lower() in answer]
    answer_rules = not absent and not present
    if absent:
        reasons.append(f"answer missing {absent}")
    if present:
        reasons.append(f"answer contains forbidden {present}")

    checks = {
        "tool_selection": tool_selection,
        "arguments": arguments,
        "trajectory": trajectory,
        "answer_rules": answer_rules,
    }
    return CheckResult(passed=all(checks.values()), checks=checks, reasons=reasons)
