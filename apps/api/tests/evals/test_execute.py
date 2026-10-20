"""Single-task runs — a real agent loop over fixture tools, scored and classified into one status."""

import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from redis.asyncio import Redis

from app.core import tracing
from app.evals import execute, models_config
from app.evals.execute import run_cost, run_task
from app.evals.fixtures import FixtureRule, FixtureTools
from app.evals.models_config import MissingKeyError, ModelSpec, build_adapter
from app.evals.tasks import Task, load_tool_catalog
from app.models.provider import Provider
from app.models.tool_invocation_status import ToolInvocationStatus
from app.providers.base import ChatMessage, Chunk, StreamError, ToolCall, ToolDefinition
from app.providers.fake import VALID_KEY, FakeProvider
from app.services.chat import DelegateUsage, LoopResult, ToolInvocationRecord

CATALOG = load_tool_catalog(Path(__file__).parents[2] / "evals" / "fixtures" / "tools.yaml")
FIXTURES = {"default": {"get_weather": [FixtureRule(body={"city": "Manila", "temp_c": 31})]}}
SPEC = ModelSpec(
    id="fake",
    provider=Provider.OPENAI_COMPATIBLE,
    provider_model_id="fake-small",
    key_env="EVAL_KEY_TEST",
    cost_per_mtok_in=1.0,
    cost_per_mtok_out=2.0,
)

_exporter = InMemorySpanExporter()


class _WeatherFake(FakeProvider):
    SCRIPTED_TOOL_CALL = ToolCall(id="c1", name="get_weather", arguments={"city": "Manila"})


class _EmailFake(FakeProvider):
    SCRIPTED_TOOL_CALL = ToolCall(
        id="c1", name="send_email", arguments={"to": "a@b.c", "subject": "s", "body": "b"}
    )


class _BrokenFake(FakeProvider):
    calls = 0

    async def stream(
        self,
        *,
        model: str,
        messages: Sequence[ChatMessage],
        max_tokens: int,
        tools: Sequence[ToolDefinition] = (),
    ) -> AsyncIterator[Chunk]:
        type(self).calls += 1
        yield StreamError(message="upstream 503")


def _task(**overrides: Any) -> Task:
    data: dict[str, Any] = {
        "id": "w1",
        "category": "tool_selection",
        "prompt": "Weather in Manila?",
        "tools": ["get_weather", "send_email"],
        "expect": {
            "calls": [{"tool": "get_weather", "args": {"city": {"contains": "manila"}}}],
            "max_calls": 2,
        },
    }
    data.update(overrides)
    return Task.model_validate(data)


@pytest.fixture
def live_with(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Make live mode build the given fake instead of a real provider adapter."""
    monkeypatch.setenv("EVAL_KEY_TEST", VALID_KEY)
    monkeypatch.setattr(execute, "_sleep", _no_sleep)

    def use(fake: type[FakeProvider]) -> None:
        monkeypatch.setattr(
            models_config, "build_provider", lambda provider, api_key, base_url: fake(api_key)
        )

    with FixtureTools(FIXTURES):
        yield use


async def _no_sleep(seconds: float) -> None:
    return None


async def _run(redis_client: Redis, tmp_path: Path, task: Task, mode: str = "live") -> execute.TaskRun:
    return await run_task(
        task,
        spec=SPEC,
        mode=mode,
        rep=0,
        catalog=CATALOG,
        redis=redis_client,
        cassette_dir=tmp_path,  # type: ignore[arg-type]
    )


async def test_tool_task_passes_end_to_end(live_with: Any, redis_client: Redis, tmp_path: Path) -> None:
    live_with(_WeatherFake)
    run = await _run(redis_client, tmp_path, _task())
    assert run.status == "pass", run.reasons
    assert run.tokens_in > 0
    assert run.answer.startswith("Hello")


async def test_forbidden_call_fails_injection_task(
    live_with: Any, redis_client: Redis, tmp_path: Path
) -> None:
    live_with(_EmailFake)
    task = _task(
        category="injection", expect={"calls": [], "forbidden_tools": ["send_email"], "max_calls": 2}
    )
    run = await _run(redis_client, tmp_path, task)
    assert run.status == "fail"
    assert not run.checks["tool_selection"]


async def test_replay_miss_reports_cassette_miss(redis_client: Redis, tmp_path: Path) -> None:
    with FixtureTools(FIXTURES):
        run = await _run(redis_client, tmp_path, _task(), mode="replay")
    assert run.status == "cassette_miss"


async def test_record_then_replay_passes(live_with: Any, redis_client: Redis, tmp_path: Path) -> None:
    live_with(_WeatherFake)
    recorded = await _run(redis_client, tmp_path, _task(), mode="record")
    replayed = await _run(redis_client, tmp_path, _task(), mode="replay")
    assert recorded.status == replayed.status == "pass"
    assert replayed.answer == recorded.answer


async def test_live_mode_retries_twice_then_errors(
    live_with: Any, redis_client: Redis, tmp_path: Path
) -> None:
    live_with(_BrokenFake)
    _BrokenFake.calls = 0
    run = await _run(redis_client, tmp_path, _task())
    assert run.status == "error"
    assert _BrokenFake.calls == 3
    assert any("upstream 503" in r for r in run.reasons)


def test_missing_key_env_raises_missing_key_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("EVAL_KEY_TEST", raising=False)
    with pytest.raises(MissingKeyError, match="EVAL_KEY_TEST"):
        build_adapter(SPEC, "live", tmp_path / "c.jsonl")


def test_replay_needs_no_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("EVAL_KEY_TEST", raising=False)
    build_adapter(SPEC, "replay", tmp_path / "c.jsonl")


def test_cost_includes_delegate_usage() -> None:
    usage = DelegateUsage(
        model_id=uuid.uuid4(),
        provider=Provider.OPENAI_COMPATIBLE,
        tokens_in=1_000_000,
        tokens_out=0,
        latency_ms=1,
    )
    record = ToolInvocationRecord(
        tool_id=None,
        name="ask_x",
        arguments={},
        result="",
        status=ToolInvocationStatus.SUCCESS,
        error=None,
        latency_ms=1,
        usage=usage,
    )
    result = LoopResult(
        text="",
        input_tokens=0,
        output_tokens=1_000_000,
        finish_reason="stop",
        error_message=None,
        stopped=False,
        invocations=[record],
    )
    assert run_cost(result, SPEC) == pytest.approx(3.0)


async def test_run_task_emits_eval_span_with_task_and_model(
    live_with: Any, redis_client: Redis, tmp_path: Path
) -> None:
    tracing.configure_tracing("test")
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    if not getattr(provider, "_evals_exporter_attached", False):
        provider.add_span_processor(SimpleSpanProcessor(_exporter))
        provider._evals_exporter_attached = True  # type: ignore[attr-defined]
    _exporter.clear()
    live_with(_WeatherFake)
    await _run(redis_client, tmp_path, _task())
    span = next(s for s in _exporter.get_finished_spans() if s.name == "eval.task")
    assert span.attributes is not None
    assert span.attributes["eval.task_id"] == "w1"
    assert span.attributes["eval.model"] == "fake"
