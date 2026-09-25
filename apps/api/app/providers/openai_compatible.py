"""Adapter for OpenAI itself, and any OpenAI-compatible server (Groq, Ollama, OpenRouter, vLLM).

The only endpoints virtually every one of these implements are `GET /models` (registering and
validating a credential) and `POST /chat/completions` with `stream: true` (chat itself).
"""

import base64
import json
from collections.abc import AsyncIterator, Sequence

import httpx

from app.providers.base import (
    ChatMessage,
    Chunk,
    CredentialCheck,
    Done,
    ModelInfo,
    StreamError,
    TextDelta,
    ToolCall,
    ToolCallRequest,
    ToolDefinition,
    Usage,
)

DEFAULT_BASE_URL = "https://api.openai.com/v1"
_TIMEOUT = 10.0
_STREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=10.0)
_EMBED_BATCH_SIZE = 100


def _content(message: ChatMessage) -> str | list[dict[str, object]]:
    """A bare string when there's nothing but text — some OpenAI-compatible servers (older
    Ollama, vLLM builds) reject the block-array form even for plain text, so it's only ever
    built when an image actually needs to ride alongside it."""
    if not message.images:
        return message.content
    blocks: list[dict[str, object]] = [{"type": "text", "text": message.content}]
    for image in message.images:
        encoded = base64.b64encode(image.data).decode("ascii")
        blocks.append(
            {"type": "image_url", "image_url": {"url": f"data:{image.mime};base64,{encoded}"}}
        )
    return blocks


def _message_payload(message: ChatMessage) -> dict[str, object]:
    """The full per-message dict OpenAI's `messages` array wants — `_content()` above only ever
    covers the `content` field, but a tool-calling turn needs `tool_calls` or `tool_call_id` too."""
    if message.role == "tool":
        return {"role": "tool", "tool_call_id": message.tool_call_id, "content": message.content}
    payload: dict[str, object] = {"role": message.role, "content": _content(message)}
    if message.tool_calls:
        payload["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
            }
            for call in message.tool_calls
        ]
    return payload


def _tools_payload(tools: Sequence[ToolDefinition]) -> list[dict[str, object]]:
    """OpenAI's function-calling shape — one wrapper object per tool."""
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            },
        }
        for tool in tools
    ]


def _parse_tool_arguments(raw: str) -> dict[str, object]:
    """Arguments arrive as a JSON-encoded string, assembled from streamed fragments — a model
    that emits malformed JSON shouldn't take the whole generation down with it."""
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


class OpenAICompatibleProvider:
    """Talks to a `/models` and `/chat/completions` shaped like OpenAI's."""

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        # Overridable only from tests, to exercise this adapter's request/response handling
        # against a fake transport instead of the real network.
        self._transport = transport

    def _client(self, timeout: httpx.Timeout | float = _TIMEOUT) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=timeout, transport=self._transport)

    def _auth_header(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    async def validate(self) -> CredentialCheck:
        """A successful models call is proof enough the key and endpoint both work."""
        try:
            await self.list_models()
        except httpx.HTTPStatusError as exc:
            return CredentialCheck(
                ok=False, detail=f"Provider rejected the request ({exc.response.status_code})."
            )
        except httpx.HTTPError as exc:
            return CredentialCheck(ok=False, detail=f"Could not reach the provider: {exc}")
        return CredentialCheck(ok=True, detail="Key validated.")

    async def list_models(self) -> list[ModelInfo]:
        """Fetch and normalize the provider's model list."""
        async with self._client() as client:
            response = await client.get(f"{self._base_url}/models", headers=self._auth_header())
            response.raise_for_status()
        data = response.json()
        return [ModelInfo(id=m["id"], display_name=m["id"]) for m in data.get("data", [])]

    async def embed(self, *, model: str, texts: list[str]) -> list[list[float]]:
        """Embed `texts`, batched to stay under OpenAI's 2048-input-per-request limit (100 is a
        conservative batch that also keeps any one request's payload small). Results come back
        sorted by their own `index` — the API doesn't guarantee response order matches input
        order — so callers can zip `texts` with the return value positionally.
        """
        vectors: list[list[float]] = []
        async with self._client() as client:
            for start in range(0, len(texts), _EMBED_BATCH_SIZE):
                batch = texts[start : start + _EMBED_BATCH_SIZE]
                response = await client.post(
                    f"{self._base_url}/embeddings",
                    headers=self._auth_header(),
                    json={"model": model, "input": batch},
                )
                response.raise_for_status()
                data = sorted(response.json()["data"], key=lambda d: d["index"])
                vectors.extend(d["embedding"] for d in data)
        return vectors

    async def stream(
        self,
        *,
        model: str,
        messages: Sequence[ChatMessage],
        max_tokens: int,
        tools: Sequence[ToolDefinition] = (),
    ) -> AsyncIterator[Chunk]:
        """Stream a chat completion, translating OpenAI's SSE chunks into normalized Chunks."""
        payload: dict[str, object] = {
            "model": model,
            "messages": [_message_payload(m) for m in messages],
            "max_tokens": max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            payload["tools"] = _tools_payload(tools)

        # Keyed by the streamed delta's own `index` — OpenAI can ask for several calls in one
        # turn, each one's id/name/arguments arriving across many chunks that share that index.
        call_fragments: dict[int, dict[str, str]] = {}

        async with self._client(_STREAM_TIMEOUT) as client, client.stream(
            "POST",
            f"{self._base_url}/chat/completions",
            headers=self._auth_header(),
            json=payload,
        ) as response:
            if response.status_code >= 400:
                body = await response.aread()
                yield StreamError(
                    f"Provider rejected the request ({response.status_code}): "
                    f"{body.decode(errors='replace')[:200]}"
                )
                return
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line.removeprefix("data:").strip()
                if data == "[DONE]" or not data:
                    continue
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choices = event.get("choices") or [{}]
                choice = choices[0]
                delta = choice.get("delta", {})
                delta_text = delta.get("content")
                if delta_text:
                    yield TextDelta(text=delta_text)
                for tc_delta in delta.get("tool_calls") or []:
                    index = tc_delta.get("index", 0)
                    function = tc_delta.get("function") or {}
                    # Some vLLM-backed providers (Nebius) mis-number a call's last argument chunk;
                    # a genuinely new call always opens with an id or name, so this continues one.
                    if index not in call_fragments and call_fragments and not (
                        tc_delta.get("id") or function.get("name")
                    ):
                        index = next(reversed(call_fragments))
                    fragment = call_fragments.setdefault(index, {"id": "", "name": "", "arguments": ""})
                    if tc_delta.get("id"):
                        fragment["id"] = tc_delta["id"]
                    if function.get("name"):
                        fragment["name"] += function["name"]
                    if function.get("arguments"):
                        fragment["arguments"] += function["arguments"]
                usage = event.get("usage")
                if usage:
                    yield Usage(
                        input_tokens=usage.get("prompt_tokens", 0),
                        output_tokens=usage.get("completion_tokens", 0),
                    )
                finish_reason = choice.get("finish_reason")
                if finish_reason == "tool_calls":
                    if call_fragments:
                        yield ToolCallRequest(
                            calls=tuple(
                                ToolCall(
                                    id=fragment["id"],
                                    # gpt-oss on some providers leaks a `<|channel|>...` marker
                                    # into the name; no real tool name can contain `<`.
                                    name=fragment["name"].split("<|", 1)[0],
                                    arguments=_parse_tool_arguments(fragment["arguments"]),
                                )
                                for fragment in call_fragments.values()
                            )
                        )
                    return
                if finish_reason:
                    yield Done(finish_reason=finish_reason)
                    return
