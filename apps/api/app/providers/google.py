"""Adapter for Google's Generative Language API (Gemini).

Not verified against the live API in this environment (no test key available) — request shape
and response parsing follow the published API (GET /v1beta/models, x-goog-api-key header,
{"models": [{"name": "models/...", "displayName", "inputTokenLimit", ...}]}), and are exercised
against a fake transport in tests the same way the OpenAI-compatible adapter is.
"""

import httpx

from app.providers.base import CredentialCheck, ModelInfo

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
_TIMEOUT = 10.0


class GoogleProvider:
    """Talks to Google's `/models` endpoint."""

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
            response = await client.get(
                f"{self._base_url}/models", headers={"x-goog-api-key": self._api_key}
            )
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
