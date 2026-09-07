"""The Anthropic adapter's request building and response parsing, against a fake transport.

See test_provider_openai_compatible.py's module docstring for why MockTransport, not a real key.
"""

import httpx

from app.providers.anthropic import AnthropicProvider


def _provider(handler, api_key: str = "sk-ant-test") -> AnthropicProvider:
    return AnthropicProvider(api_key=api_key, transport=httpx.MockTransport(handler))


async def test_sends_the_anthropic_auth_headers() -> None:
    """x-api-key and anthropic-version are both present, not a Bearer header."""
    seen_requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        return httpx.Response(200, json={"data": []})

    await _provider(handler, api_key="sk-ant-abc123").list_models()

    headers = seen_requests[0].headers
    assert headers["x-api-key"] == "sk-ant-abc123"
    assert headers["anthropic-version"] == "2023-06-01"
    assert "authorization" not in headers


async def test_uses_display_name_falling_back_to_id() -> None:
    """A model with a display_name uses it; one without falls back to its id."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "claude-opus-5", "display_name": "Claude Opus 5"},
                    {"id": "claude-legacy"},
                ]
            },
        )

    models = await _provider(handler).list_models()

    assert models[0].display_name == "Claude Opus 5"
    assert models[1].display_name == "claude-legacy"


async def test_validate_reports_rejection() -> None:
    """A 401 becomes a clear validation failure, not an unhandled exception."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "invalid x-api-key"}})

    check = await _provider(handler).validate()

    assert check.ok is False
    assert "401" in check.detail
