"""Record/replay cassettes — keyed on the exact request, replaying chunks byte for byte."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.evals.cassette import CassetteMiss, RecordingProvider, ReplayProvider, request_key
from app.providers.base import (
    ChatMessage,
    Chunk,
    ImagePart,
    StreamError,
    TextDelta,
    ToolCall,
    ToolCallRequest,
    ToolDefinition,
)
from app.providers.fake import VALID_KEY, FakeProvider

TOOLS = (ToolDefinition(name="lookup", description="d", parameters={"type": "object"}),)


class _ToolFake(FakeProvider):
    SCRIPTED_TOOL_CALL = ToolCall(id="call-xyz", name="lookup", arguments={"q": "a"})


def _messages(system: str = "sys") -> list[ChatMessage]:
    return [ChatMessage(role="system", content=system), ChatMessage(role="user", content="hi")]


def _key(messages: list[ChatMessage]) -> str:
    return request_key(model="m", messages=messages, tools=TOOLS, max_tokens=100)


async def _collect(provider: object, messages: list[ChatMessage]) -> list[Chunk]:
    return [c async for c in provider.stream(model="m", messages=messages, max_tokens=100, tools=TOOLS)]  # type: ignore[attr-defined]


def test_key_is_stable_and_order_sensitive() -> None:
    assert _key(_messages()) == _key(_messages())
    assert _key(_messages()) != _key(list(reversed(_messages())))


def test_key_changes_when_system_prompt_changes() -> None:
    assert _key(_messages("a")) != _key(_messages("b"))


def test_image_bytes_change_the_key() -> None:
    def with_image(data: bytes) -> list[ChatMessage]:
        return [ChatMessage(role="user", content="x", images=(ImagePart(mime="image/png", data=data),))]

    assert _key(with_image(b"1")) != _key(with_image(b"2"))


async def test_record_then_replay_yields_identical_chunks(tmp_path: Path) -> None:
    path = tmp_path / "m" / "t.jsonl"
    recorder = RecordingProvider(_ToolFake(api_key=VALID_KEY), path)
    first = await _collect(recorder, _messages())
    second = await _collect(recorder, _messages("after tool"))
    replay = ReplayProvider(path)
    assert await _collect(replay, _messages()) == first
    assert await _collect(replay, _messages("after tool")) == second
    assert any(isinstance(c, ToolCallRequest) and c.calls[0].id == "call-xyz" for c in first)
    assert any(isinstance(c, TextDelta) for c in second)
    assert not replay.missed


async def test_replay_miss_sets_flag_and_raises(tmp_path: Path) -> None:
    replay = ReplayProvider(tmp_path / "missing.jsonl")
    with pytest.raises(CassetteMiss):
        await _collect(replay, _messages())
    assert replay.missed


async def test_cassette_never_contains_the_api_key(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    await _collect(RecordingProvider(FakeProvider(api_key=VALID_KEY), path), _messages())
    assert VALID_KEY not in path.read_text()


async def test_an_exception_while_recording_replays_as_a_stream_error(tmp_path: Path) -> None:
    class _Dies(FakeProvider):
        async def stream(self, **kwargs: object) -> AsyncIterator[Chunk]:  # type: ignore[override]
            yield TextDelta(text="partial ")
            raise ConnectionError("connection reset")

    path = tmp_path / "t.jsonl"
    with pytest.raises(ConnectionError):
        await _collect(RecordingProvider(_Dies(api_key=VALID_KEY), path), _messages())
    replayed = await _collect(ReplayProvider(path), _messages())
    assert replayed[0] == TextDelta(text="partial ")
    assert isinstance(replayed[-1], StreamError)
    assert "connection reset" in replayed[-1].message
