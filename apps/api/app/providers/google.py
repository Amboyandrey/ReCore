"""Adapter for Google's Generative Language API (Gemini).

Not verified against the live API in this environment (no test key available) — request/response
shapes follow the published API, and are exercised against a fake transport in tests the same
way the OpenAI-compatible adapter is.
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

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
_TIMEOUT = 10.0
_STREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=10.0)
_EMBED_BATCH_SIZE = 100


def _parts(message: ChatMessage) -> list[dict[str, object]]:
    """Google's `parts` is always a list — unlike the other two adapters there's no bare-string
    form to preserve. A tool result becomes a `functionResponse` part; an assistant turn being
    replayed with tool calls becomes one `functionCall` part per call, ahead of any text."""
    if message.role == "tool":
        return [
            {
                "functionResponse": {
                    "name": message.tool_name,
                    "response": {"result": message.content},
                }
            }
        ]
    parts: list[dict[str, object]] = []
    for call in message.tool_calls:
        parts.append({"functionCall": {"name": call.name, "args": call.arguments}})
    if message.content:
        parts.append({"text": message.content})
    for image in message.images:
        encoded = base64.b64encode(image.data).decode("ascii")
        parts.append({"inline_data": {"mime_type": image.mime, "data": encoded}})
    return parts or [{"text": ""}]


def _api_role(message: ChatMessage) -> str:
    """Google's two roles: "model" for the assistant, "user" for everything else — including a
    tool result, which rides back as a user turn carrying a functionResponse part."""
    return "model" if message.role == "assistant" else "user"


def _tools_payload(tools: Sequence[ToolDefinition]) -> list[dict[str, object]]:
    """Google wants every function wrapped in one `functionDeclarations` list."""
    return [
        {
            "functionDeclarations": [
                {"name": tool.name, "description": tool.description, "parameters": tool.parameters}
                for tool in tools
            ]
        }
    ]


class GoogleProvider:
    """Talks to Google's `/models` and `:streamGenerateContent` endpoints."""

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self._transport = transport

    def _client(self, timeout: httpx.Timeout | float = _TIMEOUT) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=timeout, transport=self._transport)

    def _auth_header(self) -> dict[str, str]:
        return {"x-goog-api-key": self._api_key}

    async def validate(self) -> CredentialCheck:
        """A successful models call is proof enough the key works."""
        try:
            await self.list_models()
        except httpx.HTTPStatusError as exc:
            return CredentialCheck(
                ok=False, detail=f"Google rejected the request ({exc.response.status_code})."
            )
        except httpx.HTTPError as exc:
            return CredentialCheck(ok=False, detail=f"Could not reach Google: {exc}")
        return CredentialCheck(ok=True, detail="Key validated.")

    async def list_models(self) -> list[ModelInfo]:
        """Fetch and normalize Google's model list."""
        async with self._client() as client:
            response = await client.get(f"{self._base_url}/models", headers=self._auth_header())
            response.raise_for_status()
        data = response.json()
        models = []
        for m in data.get("models", []):
            model_id = m["name"].removeprefix("models/")
            models.append(
                ModelInfo(
                    id=model_id,
                    display_name=m.get("displayName", model_id),
                    context_window=m.get("inputTokenLimit"),
                )
            )
        return models

    async def embed(self, *, model: str, texts: list[str]) -> list[list[float]]:
        """Embed `texts` via Google's batch embedding endpoint, batched to keep any one request's
        payload reasonable — order is preserved: `embeddings[i]` in the response corresponds to
        `requests[i]` in the request, so no re-sorting is needed the way OpenAI's does.
        """
        vectors: list[list[float]] = []
        url = f"{self._base_url}/models/{model}:batchEmbedContents"
        async with self._client() as client:
            for start in range(0, len(texts), _EMBED_BATCH_SIZE):
                batch = texts[start : start + _EMBED_BATCH_SIZE]
                payload = {
                    "requests": [
                        {"model": f"models/{model}", "content": {"parts": [{"text": text}]}}
                        for text in batch
                    ]
                }
                response = await client.post(url, headers=self._auth_header(), json=payload)
                response.raise_for_status()
                data = response.json()
                vectors.extend(e["values"] for e in data["embeddings"])
        return vectors

    async def stream(
        self,
        *,
        model: str,
        messages: Sequence[ChatMessage],
        max_tokens: int,
        tools: Sequence[ToolDefinition] = (),
    ) -> AsyncIterator[Chunk]:
        """Stream a chat completion, translating Google's SSE chunks into normalized Chunks.

        Google takes a system prompt as its own top-level field, and calls the assistant's role
        "model" rather than "assistant" in the conversation history.
        """
        system_prompt = next((m.content for m in messages if m.role == "system"), None)
        payload: dict[str, object] = {
            "contents": [
                {"role": _api_role(m), "parts": _parts(m)} for m in messages if m.role != "system"
            ],
            "generationConfig": {"maxOutputTokens": max_tokens},
        }
        if system_prompt is not None:
            payload["systemInstruction"] = {"parts": [{"text": system_prompt}]}
        if tools:
            payload["tools"] = _tools_payload(tools)

        # Google has no call id of its own — one is synthesized here purely so this adapter's
        # output fits the same ToolCall shape every other adapter produces. It never round-trips
        # back to Google: a tool result there is matched by name (see _parts above), not id.
        call_index = 0

        url = f"{self._base_url}/models/{model}:streamGenerateContent"
        async with self._client(_STREAM_TIMEOUT) as client, client.stream(
            "POST", url, headers=self._auth_header(), params={"alt": "sse"}, json=payload
        ) as response:
            if response.status_code >= 400:
                body = await response.aread()
                yield StreamError(
                    f"Google rejected the request ({response.status_code}): "
                    f"{body.decode(errors='replace')[:200]}"
                )
                return
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line.removeprefix("data:").strip()
                if not data:
                    continue
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue

                candidates = event.get("candidates") or []
                finish_reason = None
                calls: list[ToolCall] = []
                if candidates:
                    candidate = candidates[0]
                    for part in candidate.get("content", {}).get("parts", []):
                        text = part.get("text")
                        if text:
                            yield TextDelta(text=text)
                        function_call = part.get("functionCall")
                        if function_call:
                            calls.append(
                                ToolCall(
                                    id=f"{function_call.get('name', 'tool')}-{call_index}",
                                    name=function_call.get("name", ""),
                                    arguments=function_call.get("args") or {},
                                )
                            )
                            call_index += 1
                    finish_reason = candidate.get("finishReason")

                usage = event.get("usageMetadata")
                if usage:
                    yield Usage(
                        input_tokens=usage.get("promptTokenCount", 0),
                        output_tokens=usage.get("candidatesTokenCount", 0),
                    )
                if calls:
                    yield ToolCallRequest(calls=tuple(calls))
                    return
                if finish_reason:
                    yield Done(finish_reason=finish_reason)
                    return
