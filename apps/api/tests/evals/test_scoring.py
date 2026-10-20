"""Hard scoring — pure checks over one loop result, never raising on odd model output."""

from typing import Any

from app.evals.scoring import flatten_calls, match_value, score_hard
from app.evals.tasks import Matcher, Task
from app.models.tool_invocation_status import ToolInvocationStatus
from app.services.chat import TOOL_LOOP_EXHAUSTED, LoopResult, ToolInvocationRecord


def _call(
    name: str, args: dict[str, object], children: list[ToolInvocationRecord] | None = None
) -> ToolInvocationRecord:
    return ToolInvocationRecord(
        tool_id=None,
        name=name,
        arguments=args,
        result="ok",
        status=ToolInvocationStatus.SUCCESS,
        error=None,
        latency_ms=1,
        children=children or [],
    )


def _result(
    calls: list[ToolInvocationRecord], text: str = "Manila is 31C.", error: str | None = None
) -> LoopResult:
    return LoopResult(
        text=text,
        input_tokens=10,
        output_tokens=5,
        finish_reason="stop",
        error_message=error,
        stopped=False,
        invocations=calls,
    )


def _task(**expect: Any) -> Task:
    base: dict[str, Any] = {"max_calls": 4}
    base.update(expect)
    return Task.model_validate(
        {
            "id": "t1",
            "category": "multi_step",
            "prompt": "p",
            "tools": ["get_weather", "send_email"],
            "expect": base,
        }
    )


def _weather(city: str) -> dict[str, Any]:
    return {"tool": "get_weather", "args": {"city": {"contains": city}}}


def test_contains_is_case_insensitive() -> None:
    assert match_value(Matcher(contains="manila"), "Manila, PH")


def test_approx_rejects_non_numbers() -> None:
    assert not match_value(Matcher(approx=30, tol=0.5), "hot")
    assert match_value(Matcher(approx=30, tol=0.5), 30.4)


def test_regex_and_equals_and_any() -> None:
    assert match_value(Matcher(regex=r"^\d{3}$"), 123)
    assert match_value(Matcher(equals=3), 3)
    assert not match_value(Matcher(equals="3"), 3)
    assert match_value(Matcher(any=True), None)


def test_any_order_matches_each_expected_call_once() -> None:
    task = _task(calls=[_weather("manila"), _weather("manila")])
    result = score_hard(task, _result([_call("get_weather", {"city": "Manila"})]))
    assert not result.checks["arguments"]
    assert not result.passed


def test_any_order_finds_assignment_regardless_of_order() -> None:
    task = _task(calls=[_weather("manila"), _weather("vienna")])
    calls = [_call("get_weather", {"city": "Vienna"}), _call("get_weather", {"city": "Manila"})]
    assert score_hard(task, _result(calls)).passed


def test_subsequence_allows_gaps_but_not_reordering() -> None:
    task = _task(calls=[_weather("manila"), _weather("vienna")], order="subsequence")
    gap = [
        _call("get_weather", {"city": "Manila"}),
        _call("get_weather", {"city": "Oslo"}),
        _call("get_weather", {"city": "Vienna"}),
    ]
    assert score_hard(task, _result(gap)).passed
    reordered = [_call("get_weather", {"city": "Vienna"}), _call("get_weather", {"city": "Manila"})]
    assert not score_hard(task, _result(reordered)).passed


def test_strict_requires_same_length_and_positions() -> None:
    task = _task(calls=[_weather("manila")], order="strict")
    assert score_hard(task, _result([_call("get_weather", {"city": "Manila"})])).passed
    extra = [_call("get_weather", {"city": "Manila"}), _call("get_weather", {"city": "Oslo"})]
    assert not score_hard(task, _result(extra)).passed


def test_forbidden_tool_fails_tool_selection() -> None:
    task = _task(calls=[], forbidden_tools=["send_email"])
    result = score_hard(task, _result([_call("send_email", {"to": "x@example.com"})]))
    assert not result.checks["tool_selection"]
    assert any("send_email" in r for r in result.reasons)


def test_unknown_tool_and_non_dict_arguments_fail_without_raising() -> None:
    task = _task(calls=[_weather("manila")])
    odd = _call("get_weather", {})
    odd.arguments = "not a dict"  # type: ignore[assignment]
    result = score_hard(task, _result([_call("teleport", {"x": 1}), odd]))
    assert not result.passed


def test_exhausted_loop_fails_trajectory() -> None:
    task = _task(calls=[_weather("manila")])
    result = score_hard(task, _result([_call("get_weather", {"city": "Manila"})], error=TOOL_LOOP_EXHAUSTED))
    assert not result.checks["trajectory"]


def test_max_calls_counts_flattened_calls() -> None:
    task = _task(calls=[], max_calls=1)
    nested = _call(
        "ask_helper", {"task": "x"}, children=[_call("ask_helper/get_weather", {"city": "Manila"})]
    )
    result = score_hard(task, _result([nested]))
    assert not result.checks["trajectory"]


def test_delegate_children_are_scored_with_prefix() -> None:
    child = {"tool": "ask_helper/get_weather", "args": {"city": {"contains": "manila"}}}
    task = _task(calls=[{"tool": "ask_helper"}, child])
    nested = _call(
        "ask_helper", {"task": "x"}, children=[_call("ask_helper/get_weather", {"city": "Manila"})]
    )
    assert [c.name for c in flatten_calls([nested])] == ["ask_helper", "ask_helper/get_weather"]
    assert score_hard(task, _result([nested])).passed


def test_no_tool_task_passes_with_zero_calls() -> None:
    task = _task(calls=[], max_calls=0)
    assert score_hard(task, _result([])).passed


def test_answer_rules_are_case_insensitive() -> None:
    task = _task(calls=[], answer={"must_contain": ["manila"], "must_not_contain": ["error"]})
    assert score_hard(task, _result([], text="MANILA is warm")).checks["answer_rules"]
    assert not score_hard(task, _result([], text="Error: Manila")).checks["answer_rules"]
