"""Agent-loop spans — one per provider round trip and per tool call, never carrying payloads."""

import uuid
from collections.abc import Iterator

import httpx
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from redis.asyncio import Redis

from app.core import tracing
from app.models import LLMModel, Tool, ToolKind
from app.models.provider import Provider
from app.providers.base import ToolCall
from app.providers.fake import VALID_KEY, FakeProvider
from app.services.assistant_runtime import AssistantTurn
from app.services.chat import run_assistant_task
from app.tools import http_tool

SECRET_MARKER = "zz-secret-payload-zz"

_exporter = InMemorySpanExporter()


class _LookupFake(FakeProvider):
    SCRIPTED_TOOL_CALL = ToolCall(id="call-1", name="lookup", arguments={"q": SECRET_MARKER})


@pytest.fixture
def spans(monkeypatch: pytest.MonkeyPatch) -> Iterator[InMemorySpanExporter]:
    """Route spans into memory and serve the lookup tool from a fake transport."""
    tracing.configure_tracing("test")
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    if not getattr(provider, "_test_exporter_attached", False):
        provider.add_span_processor(SimpleSpanProcessor(_exporter))
        provider._test_exporter_attached = True  # type: ignore[attr-defined]
    _exporter.clear()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=f"result {SECRET_MARKER}")

    monkeypatch.setattr(http_tool, "_transport", httpx.MockTransport(handler))
    monkeypatch.setattr(http_tool, "assert_safe_base_url", lambda url: None)
    yield _exporter


def _turn() -> AssistantTurn:
    tool = Tool(
        id=uuid.uuid4(),
        name="lookup",
        description="Look something up.",
        parameters={"type": "object", "properties": {"q": {"type": "string"}}},
        kind=ToolKind.HTTP,
        method="POST",
        url="https://tools.example.com/lookup",
    )
    model = LLMModel(id=uuid.uuid4(), provider_model_id="fake-small", display_name="Fake Small")
    return AssistantTurn(
        system_prompt=f"system {SECRET_MARKER}",
        adapter=_LookupFake(api_key=VALID_KEY),
        provider=Provider.OPENAI_COMPATIBLE,
        model=model,
        tools=[tool],
        delegates=[],
        sources=[],
        memory_assistant_id=None,
    )


async def _run(redis_client: Redis) -> None:
    await run_assistant_task(
        redis=redis_client,
        stream_id=f"trace:{uuid.uuid4()}",
        workspace_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        turn=_turn(),
        task=f"find {SECRET_MARKER}",
        emit_deltas=False,
    )


async def test_each_round_trip_and_tool_call_gets_a_span(
    spans: InMemorySpanExporter, redis_client: Redis
) -> None:
    await _run(redis_client)
    finished = spans.get_finished_spans()
    names = [s.name for s in finished]
    assert names.count("gen_ai.chat") == 2
    assert names.count("gen_ai.execute_tool") == 1
    chat_span = next(s for s in finished if s.name == "gen_ai.chat")
    assert chat_span.attributes is not None
    assert chat_span.attributes["gen_ai.request.model"] == "fake-small"
    assert chat_span.attributes["recore.tool_calls_requested"] == 1
    tool_span = next(s for s in finished if s.name == "gen_ai.execute_tool")
    assert tool_span.attributes is not None
    assert tool_span.attributes["gen_ai.tool.name"] == "lookup"
    assert tool_span.attributes["recore.tool.status"] == "success"


async def test_spans_never_carry_prompt_or_tool_payloads(
    spans: InMemorySpanExporter, redis_client: Redis
) -> None:
    await _run(redis_client)
    finished = spans.get_finished_spans()
    assert finished
    for span in finished:
        for value in (span.attributes or {}).values():
            assert SECRET_MARKER not in str(value)


def test_otlp_exporter_is_used_when_endpoint_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    added: list[object] = []
    monkeypatch.setattr(tracing, "_configured", False)
    monkeypatch.setattr(tracing.trace, "set_tracer_provider", lambda provider: None)
    monkeypatch.setattr(TracerProvider, "add_span_processor", lambda self, p: added.append(p))
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    tracing.configure_tracing("test")
    assert any(
        isinstance(p, BatchSpanProcessor) and isinstance(p.span_exporter, OTLPSpanExporter) for p in added
    )
    monkeypatch.setattr(tracing, "_configured", True)
