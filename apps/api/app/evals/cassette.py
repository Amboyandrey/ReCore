"""Provider-level record/replay — wraps any LLMProvider, so one mechanism covers every adapter.

A cassette is a JSONL file of `{"key", "chunks"}` lines: `key` hashes the exact request (model,
messages, tools, max_tokens) and `chunks` are the normalized stream chunks it produced. Replay
serves those chunks back for the same request, so PR CI needs no network and no secrets; a request
the cassette has never seen is a miss, not a silent live call.
"""

import hashlib
import json
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

from app.providers.base import (
    ChatMessage,
    Chunk,
    CredentialCheck,
    Done,
    LLMProvider,
    ModelInfo,
    StreamError,
    TextDelta,
    ToolCall,
    ToolCallRequest,
    ToolDefinition,
    Usage,
)


class CassetteMiss(Exception):
    """Replay was asked for a request it has no recording of."""


def _message_json(m: ChatMessage) -> dict[str, Any]:
    return {
        "role": m.role,
        "content": m.content,
        "images": [{"mime": i.mime, "sha256": hashlib.sha256(i.data).hexdigest()} for i in m.images],
        "tool_calls": [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in m.tool_calls],
        "tool_call_id": m.tool_call_id,
        "tool_name": m.tool_name,
    }


def request_key(
    *, model: str, messages: Sequence[ChatMessage], tools: Sequence[ToolDefinition], max_tokens: int
) -> str:
    """SHA-256 over the canonical JSON of everything the provider is sent."""
    payload = {
        "model": model,
        "messages": [_message_json(m) for m in messages],
        "tools": [{"name": t.name, "description": t.description, "parameters": t.parameters} for t in tools],
        "max_tokens": max_tokens,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def chunk_to_json(chunk: Chunk) -> dict[str, Any]:
    if isinstance(chunk, TextDelta):
        return {"type": "text", "text": chunk.text}
    if isinstance(chunk, ToolCallRequest):
        calls = [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in chunk.calls]
        return {"type": "tool_calls", "calls": calls}
    if isinstance(chunk, Usage):
        return {"type": "usage", "input_tokens": chunk.input_tokens, "output_tokens": chunk.output_tokens}
    if isinstance(chunk, Done):
        return {"type": "done", "finish_reason": chunk.finish_reason}
    return {"type": "error", "message": chunk.message}


def chunk_from_json(data: dict[str, Any]) -> Chunk:
    kind = data["type"]
    if kind == "text":
        return TextDelta(text=data["text"])
    if kind == "tool_calls":
        return ToolCallRequest(calls=tuple(ToolCall(**c) for c in data["calls"]))
    if kind == "usage":
        return Usage(input_tokens=data["input_tokens"], output_tokens=data["output_tokens"])
    if kind == "done":
        return Done(finish_reason=data["finish_reason"])
    return StreamError(message=data["message"])


class RecordingProvider:
    """Passes every call through to the real adapter and appends what it streamed to the cassette."""

    def __init__(self, inner: LLMProvider, path: Path) -> None:
        self._inner = inner
        self._path = path

    async def validate(self) -> CredentialCheck:
        return await self._inner.validate()

    async def list_models(self) -> list[ModelInfo]:
        return await self._inner.list_models()

    async def stream(
        self,
        *,
        model: str,
        messages: Sequence[ChatMessage],
        max_tokens: int,
        tools: Sequence[ToolDefinition] = (),
    ) -> AsyncIterator[Chunk]:
        key = request_key(model=model, messages=messages, tools=tools, max_tokens=max_tokens)
        chunks: list[Chunk] = []
        try:
            async for chunk in self._inner.stream(
                model=model, messages=messages, max_tokens=max_tokens, tools=tools
            ):
                chunks.append(chunk)
                yield chunk
        except Exception as exc:
            # Replay then ends the stream the same way the agent loop saw it end: as a failure.
            chunks.append(StreamError(message=f"{type(exc).__name__}: {exc}"))
            raise
        finally:
            # Written even when the consumer stops early, so replay reproduces exactly what it saw.
            self._path.parent.mkdir(parents=True, exist_ok=True)
            line = {"key": key, "chunks": [chunk_to_json(c) for c in chunks]}
            with self._path.open("a") as f:
                f.write(json.dumps(line, sort_keys=True) + "\n")


class ReplayProvider:
    """Serves recorded chunks for known requests; anything else sets `missed` and raises CassetteMiss."""

    def __init__(self, path: Path) -> None:
        self.missed = False
        # Identical requests (say, two delegations with the same task) replay in recorded order.
        self._entries: dict[str, deque[list[dict[str, Any]]]] = defaultdict(deque)
        if path.exists():
            for raw in path.read_text().splitlines():
                if raw.strip():
                    entry = json.loads(raw)
                    self._entries[entry["key"]].append(entry["chunks"])

    async def validate(self) -> CredentialCheck:
        return CredentialCheck(ok=True, detail="replay")

    async def list_models(self) -> list[ModelInfo]:
        return []

    async def stream(
        self,
        *,
        model: str,
        messages: Sequence[ChatMessage],
        max_tokens: int,
        tools: Sequence[ToolDefinition] = (),
    ) -> AsyncIterator[Chunk]:
        key = request_key(model=model, messages=messages, tools=tools, max_tokens=max_tokens)
        recorded = self._entries.get(key)
        if not recorded:
            self.missed = True
            raise CassetteMiss(key)
        chunks = recorded.popleft() if len(recorded) > 1 else recorded[0]
        for data in chunks:
            yield chunk_from_json(data)
