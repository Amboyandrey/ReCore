"""Adapter for Anthropic's API.

Not verified against the live API in this environment (no test key available) — request/response
shapes follow Anthropic's published Messages and Models APIs, and are exercised against a fake
transport in tests the same way the OpenAI-compatible adapter is.
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
    Usage,
)

DEFAULT_BASE_URL = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"
_TIMEOUT = 10.0
_STREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=10.0)


def _content(message: ChatMessage) -> str | list[dict[str, object]]:
    """A bare string when there's nothing but text; Anthropic's block form is only ever built
    once an image actually needs to ride alongside it."""
    if not message.images:
        return message.content
    blocks: list[dict[str, object]] = [{"type": "text", "text": message.content}]
    for image in message.images:
        encoded = base64.b64encode(image.data).decode("ascii")
        blocks.append(
            {
                "type": "image",
                "source": {"type": "base64", "media_type": image.mime, "data": encoded},
            }
        )
    return blocks


class AnthropicProvider:
    """Talks to Anthropic's `/models` and `/messages` endpoints."""

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

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self._api_key, "anthropic-version": ANTHROPIC_VERSION}

    async def validate(self) -> CredentialCheck:
        """A successful models call is proof enough the key works."""
        try:
            await self.list_models()
        except httpx.HTTPStatusError as exc:
            return CredentialCheck(
                ok=False, detail=f"Anthropic rejected the request ({exc.response.status_code})."
            )
        except httpx.HTTPError as exc:
            return CredentialCheck(ok=False, detail=f"Could not reach Anthropic: {exc}")
        return CredentialCheck(ok=True, detail="Key validated.")

    async def list_models(self) -> list[ModelInfo]:
        """Fetch and normalize Anthropic's model list."""
        async with self._client() as client:
            response = await client.get(f"{self._base_url}/models", headers=self._headers())
            response.raise_for_status()
        data = response.json()
        return [
            ModelInfo(id=m["id"], display_name=m.get("display_name", m["id"]))
            for m in data.get("data", [])
        ]

    async def stream(
        self, *, model: str, messages: Sequence[ChatMessage], max_tokens: int
    ) -> AsyncIterator[Chunk]:
        """Stream a chat completion, translating Anthropic's SSE events into normalized Chunks.

        Anthropic takes a system prompt as its own top-level field, not a role in the messages
        array — so a "system" message here is pulled out rather than sent as a turn.
        """
        system_prompt = next((m.content for m in messages if m.role == "system"), None)
        payload: dict[str, object] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": m.role, "content": _content(m)} for m in messages if m.role != "system"
            ],
            "stream": True,
        }
        if system_prompt is not None:
            payload["system"] = system_prompt

        input_tokens = 0
        output_tokens = 0
        stop_reason = "end_turn"

        async with self._client(_STREAM_TIMEOUT) as client, client.stream(
            "POST", f"{self._base_url}/messages", headers=self._headers(), json=payload
        ) as response:
            if response.status_code >= 400:
                body = await response.aread()
                yield StreamError(
                    f"Anthropic rejected the request ({response.status_code}): "
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

                event_type = event.get("type")
                if event_type == "content_block_delta":
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        yield TextDelta(text=delta.get("text", ""))
                elif event_type == "message_start":
                    input_tokens = event.get("message", {}).get("usage", {}).get(
                        "input_tokens", 0
                    )
                elif event_type == "message_delta":
                    output_tokens = event.get("usage", {}).get("output_tokens", 0)
                    stop_reason = event.get("delta", {}).get("stop_reason") or stop_reason
                elif event_type == "message_stop":
                    yield Usage(input_tokens=input_tokens, output_tokens=output_tokens)
                    yield Done(finish_reason=stop_reason)
                    return
                elif event_type == "error":
                    yield StreamError(event.get("error", {}).get("message", "Unknown error"))
                    return
