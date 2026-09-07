"""The Google adapter's request building and response parsing, against a fake transport.

See test_provider_openai_compatible.py's module docstring for why MockTransport, not a real key.
"""

import httpx

from app.providers.google import GoogleProvider


def _provider(handler, api_key: str = "test-key") -> GoogleProvider:
    return GoogleProvider(api_key=api_key, transport=httpx.MockTransport(handler))


async def test_sends_the_key_as_a_header_not_a_query_param() -> None:
    """The key never lands in the URL, where it's far more likely to leak into logs."""
    seen_requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        return httpx.Response(200, json={"models": []})

    await _provider(handler, api_key="AIzaTest123").list_models()

    request = seen_requests[0]
    assert request.headers["x-goog-api-key"] == "AIzaTest123"
    assert "AIzaTest123" not in str(request.url)


async def test_strips_the_models_prefix_and_maps_context_window() -> None:
    """Google names models 'models/gemini-...' and calls context length inputTokenLimit."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "models": [
                    {
                        "name": "models/gemini-2.5-pro",
                        "displayName": "Gemini 2.5 Pro",
                        "inputTokenLimit": 2_000_000,
                    }
                ]
            },
        )

    models = await _provider(handler).list_models()

    assert models[0].id == "gemini-2.5-pro"
    assert models[0].display_name == "Gemini 2.5 Pro"
    assert models[0].context_window == 2_000_000


async def test_validate_reports_rejection() -> None:
    """A 400 (Google's response to a bad key) becomes a clear validation failure."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "API key not valid"}})

    check = await _provider(handler).validate()

    assert check.ok is False
    assert "400" in check.detail
