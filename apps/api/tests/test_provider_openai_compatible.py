"""The OpenAI-compatible adapter's request building and response parsing, against a fake transport.

Using httpx.MockTransport rather than a real socket: what needs verifying is this adapter's own
code (does it send the right auth header, parse the right shape, handle a rejection gracefully),
not whether a TCP connection can be opened — and it exercises the exact same AsyncClient code
path a real request would.
"""

import httpx
import pytest

from app.providers.openai_compatible import OpenAICompatibleProvider


def _provider(handler, base_url: str | None = None, api_key: str = "test-key") -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        api_key=api_key, base_url=base_url, transport=httpx.MockTransport(handler)
    )


async def test_list_models_sends_bearer_auth_and_parses_ids() -> None:
    """The request carries the key as a Bearer token, and every model id comes back."""
    seen_requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        return httpx.Response(200, json={"data": [{"id": "gpt-5"}, {"id": "gpt-5-mini"}]})

    provider = _provider(handler, api_key="sk-abc123")
    models = await provider.list_models()

    assert [m.id for m in models] == ["gpt-5", "gpt-5-mini"]
    assert seen_requests[0].headers["authorization"] == "Bearer sk-abc123"


async def test_custom_base_url_is_used_with_trailing_slash_stripped() -> None:
    """A workspace-supplied base URL (e.g. a local Ollama) is hit instead of api.openai.com."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://localhost:11434/v1/models"
        return httpx.Response(200, json={"data": []})

    provider = _provider(handler, base_url="http://localhost:11434/v1/")
    await provider.list_models()


async def test_validate_reports_the_status_code_on_rejection() -> None:
    """An unauthorized response becomes a clear, specific validation failure."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid api key"})

    check = await _provider(handler).validate()

    assert check.ok is False
    assert "401" in check.detail


async def test_validate_reports_unreachable_providers() -> None:
    """A transport-level failure (DNS, connection refused, ...) is reported, not raised."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    check = await _provider(handler).validate()

    assert check.ok is False
    assert "reach" in check.detail.lower()


async def test_validate_succeeds_when_models_are_listed() -> None:
    """A working key against a reachable endpoint validates cleanly."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "llama3"}]})

    check = await _provider(handler).validate()

    assert check.ok is True


async def test_list_models_raises_for_a_rejected_key() -> None:
    """list_models doesn't swallow errors — only validate() translates them into a CredentialCheck."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={})

    with pytest.raises(httpx.HTTPStatusError):
        await _provider(handler).list_models()
