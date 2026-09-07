"""Adapter for Anthropic's API.

Not verified against the live API in this environment (no test key available) — request shape
and response parsing follow Anthropic's published Models API (GET /v1/models, x-api-key +
anthropic-version headers, {"data": [{"id", "display_name", ...}]}), and are exercised against a
fake transport in tests the same way the OpenAI-compatible adapter is.
"""

import httpx

from app.providers.base import CredentialCheck, ModelInfo

DEFAULT_BASE_URL = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"
_TIMEOUT = 10.0


class AnthropicProvider:
    """Talks to Anthropic's `/models` endpoint."""

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

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=_TIMEOUT, transport=self._transport)

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
