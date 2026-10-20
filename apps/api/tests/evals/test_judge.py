"""LLM judge — verdict parsing that never crashes, and calibration math against hand labels."""

from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from redis.asyncio import Redis

from app.evals import execute, models_config
from app.evals.execute import run_task
from app.evals.fixtures import FixtureRule, FixtureTools
from app.evals.judge import Judge, calibration_stats, parse_verdict
from app.evals.models_config import ModelSpec
from app.evals.tasks import Task, load_tool_catalog
from app.models.provider import Provider
from app.providers.base import ChatMessage, Chunk, Done, TextDelta, ToolCall, ToolDefinition
from app.providers.fake import VALID_KEY, FakeProvider

CATALOG = load_tool_catalog(Path(__file__).parents[2] / "evals" / "fixtures" / "tools.yaml")
SPEC = ModelSpec(
    id="fake",
    provider=Provider.OPENAI_COMPATIBLE,
    provider_model_id="fake-small",
    key_env="EVAL_KEY_TEST",
    cost_per_mtok_in=0,
    cost_per_mtok_out=0,
)


class _Says(FakeProvider):
    """Replies with a fixed text, recording what it was offered."""

    reply = ""

    async def stream(
        self,
        *,
        model: str,
        messages: Sequence[ChatMessage],
        max_tokens: int,
        tools: Sequence[ToolDefinition] = (),
    ) -> AsyncIterator[Chunk]:
        self.last_messages, self.last_tools = messages, tools
        yield TextDelta(text=self.reply)
        yield Done(finish_reason="stop")


def test_parse_bare_fenced_and_embedded_json() -> None:
    assert parse_verdict('{"pass": true, "reason": "ok"}') == (True, "ok")
    assert parse_verdict('```json\n{"pass": false, "reason": "no"}\n```') == (False, "no")
    assert parse_verdict('Verdict: {"pass": true, "reason": "fine"} done') == (True, "fine")


def test_parse_returns_none_for_prose_or_missing_pass() -> None:
    assert parse_verdict("looks good to me") is None
    assert parse_verdict('{"reason": "x"}') is None
    assert parse_verdict('{"pass": "yes"}') is None
    assert parse_verdict("") is None


def test_kappa_known_value() -> None:
    # 10 pairs: 4 both-pass, 4 both-fail, 1 judge-only pass, 1 human-only pass -> po 0.8, pe 0.5.
    pairs = [(True, True)] * 4 + [(False, False)] * 4 + [(True, False), (False, True)]
    report = calibration_stats(pairs)
    assert report.n == 10
    assert report.agreement == pytest.approx(0.8)
    assert report.kappa == pytest.approx(0.6)
    assert report.false_pass_rate == pytest.approx(0.2)


def test_kappa_degenerate_cases() -> None:
    assert calibration_stats([(True, True)] * 3).kappa == 1.0
    assert calibration_stats([(True, False)] * 3).kappa == 0.0


def test_false_pass_rate_without_human_fails_is_zero() -> None:
    assert calibration_stats([(True, True), (False, True)]).false_pass_rate == 0.0


async def test_grade_uses_no_tools_and_handles_empty_answer() -> None:
    fake = _Says(api_key=VALID_KEY)
    fake.reply = '{"pass": true, "reason": "ok"}'
    verdict = await Judge(fake, SPEC).grade(prompt="p", rubric="r", answer="")
    assert verdict == (True, "ok")
    assert fake.last_tools == ()
    assert "(empty answer)" in fake.last_messages[-1].content


class _Weather(FakeProvider):
    SCRIPTED_TOOL_CALL = ToolCall(id="c1", name="get_weather", arguments={"city": "Manila"})


async def _no_sleep(seconds: float) -> None:
    return None


@pytest.mark.parametrize(
    ("reply", "status"), [('{"pass": false, "reason": "vague"}', "fail"), ("hmm", "error")]
)
async def test_run_task_fails_on_judge_fail_and_errors_on_no_verdict(
    monkeypatch: pytest.MonkeyPatch, redis_client: Redis, tmp_path: Path, reply: str, status: str
) -> None:
    monkeypatch.setenv("EVAL_KEY_TEST", VALID_KEY)
    monkeypatch.setattr(execute, "_sleep", _no_sleep)
    judge = _Says(api_key=VALID_KEY)
    judge.reply = reply

    def build(provider: Any, api_key: str, base_url: str | None) -> FakeProvider:
        return judge if base_url == "judge" else _Weather(api_key)

    monkeypatch.setattr(models_config, "build_provider", build)
    task = Task.model_validate(
        {
            "id": "j1",
            "category": "tool_selection",
            "prompt": "Weather in Manila?",
            "tools": ["get_weather"],
            "expect": {
                "calls": [{"tool": "get_weather"}],
                "max_calls": 2,
                "answer": {"judge": "Gives a temp."},
            },
        }
    )
    with FixtureTools({"default": {"get_weather": [FixtureRule(body="31C")]}}):
        run = await run_task(
            task,
            spec=SPEC,
            mode="live",
            rep=0,
            catalog=CATALOG,
            redis=redis_client,
            cassette_dir=tmp_path,
            judge_spec=SPEC.model_copy(update={"id": "judge", "base_url": "judge"}),
        )
    assert run.status == status
    if status == "fail":
        assert run.checks["judge"] is False
        assert any("vague" in r for r in run.reasons)
