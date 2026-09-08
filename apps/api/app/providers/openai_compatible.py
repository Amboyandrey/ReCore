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
    Usage,
)

DEFAULT_BASE_URL = "https://api.openai.com/v1"
_TIMEOUT = 10.0
_STREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=10.0)


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

    async def stream(
        self, *, model: str, messages: Sequence[ChatMessage], max_tokens: int
    ) -> AsyncIterator[Chunk]:
        """Stream a chat completion, translating OpenAI's SSE chunks into normalized Chunks."""
        payload = {
            "model": model,
            "messages": [{"role": m.role, "content": _content(m)} for m in messages],
            "max_tokens": max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
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
                delta_text = choice.get("delta", {}).get("content")
                if delta_text:
                    yield TextDelta(text=delta_text)
                usage = event.get("usage")
                if usage:
                    yield Usage(
                        input_tokens=usage.get("prompt_tokens", 0),
                        output_tokens=usage.get("completion_tokens", 0),
                    )
                finish_reason = choice.get("finish_reason")
                if finish_reason:
                    yield Done(finish_reason=finish_reason)
                    return
