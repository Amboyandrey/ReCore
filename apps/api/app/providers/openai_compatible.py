"""Adapter for OpenAI itself, and any OpenAI-compatible server (Groq, Ollama, OpenRouter, vLLM).

The only endpoint virtually every one of these implements is `GET /models` — that's the whole
surface this adapter needs for registering and validating a credential.
"""

import httpx

from app.providers.base import CredentialCheck, ModelInfo

DEFAULT_BASE_URL = "https://api.openai.com/v1"
_TIMEOUT = 10.0


class OpenAICompatibleProvider:
    """Talks to a `/models` endpoint shaped like OpenAI's."""

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

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=_TIMEOUT, transport=self._transport)

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
            response = await client.get(
                f"{self._base_url}/models", headers={"Authorization": f"Bearer {self._api_key}"}
            )
            response.raise_for_status()
        data = response.json()
        return [ModelInfo(id=m["id"], display_name=m["id"]) for m in data.get("data", [])]
