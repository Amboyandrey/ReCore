"""The embeddings side of the provider adapters, against a fake transport — same reasoning as
test_provider_openai_compatible.py: what needs verifying is request/response handling, not
whether a real socket can be opened."""

import json

import httpx
import pytest

from app.core.errors import EmbeddingsNotSupported
from app.models import Provider
from app.providers.google import GoogleProvider
from app.providers.openai_compatible import OpenAICompatibleProvider
from app.providers.registry import build_embedding_provider, supports_embeddings


async def test_openai_embed_batches_over_100_texts_and_preserves_order() -> None:
    """201 texts split into three requests of <=100, and the response's own `index` (not
    request order) determines which vector lands where — OpenAI doesn't guarantee response
    order matches input order."""
    seen_batches: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen_batches.append(body["input"])
        # Return in reverse index order, to prove the adapter re-sorts rather than trusting order.
        n = len(body["input"])
        data = [{"index": i, "embedding": [float(i)]} for i in reversed(range(n))]
        return httpx.Response(200, json={"data": data})

    provider = OpenAICompatibleProvider(api_key="test-key", transport=httpx.MockTransport(handler))
    texts = [f"text-{i}" for i in range(201)]

    vectors = await provider.embed(model="text-embedding-3-small", texts=texts)

    assert [len(b) for b in seen_batches] == [100, 100, 1]
    assert vectors[0] == [0.0]
    assert vectors[99] == [99.0]


async def test_openai_embed_sends_model_and_bearer_auth() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer sk-test"
        body = json.loads(request.content)
        assert body == {"model": "text-embedding-3-small", "input": ["hello"]}
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1, 0.2]}]})

    provider = OpenAICompatibleProvider(api_key="sk-test", transport=httpx.MockTransport(handler))
    vectors = await provider.embed(model="text-embedding-3-small", texts=["hello"])

    assert vectors == [[0.1, 0.2]]


async def test_google_embed_uses_batch_endpoint_and_preserves_order() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(":batchEmbedContents")
        assert request.headers["x-goog-api-key"] == "goog-key"
        body = json.loads(request.content)
        assert [r["content"]["parts"][0]["text"] for r in body["requests"]] == ["a", "b"]
        return httpx.Response(200, json={"embeddings": [{"values": [1.0]}, {"values": [2.0]}]})

    provider = GoogleProvider(api_key="goog-key", transport=httpx.MockTransport(handler))
    vectors = await provider.embed(model="text-embedding-004", texts=["a", "b"])

    assert vectors == [[1.0], [2.0]]


async def test_google_embed_batches_over_100_texts() -> None:
    seen_batches: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen_batches.append(len(body["requests"]))
        return httpx.Response(
            200, json={"embeddings": [{"values": [0.0]} for _ in body["requests"]]}
        )

    provider = GoogleProvider(api_key="k", transport=httpx.MockTransport(handler))
    await provider.embed(model="text-embedding-004", texts=[f"t{i}" for i in range(150)])

    assert seen_batches == [100, 50]


def test_supports_embeddings_excludes_anthropic() -> None:
    assert supports_embeddings(Provider.OPENAI) is True
    assert supports_embeddings(Provider.OPENAI_COMPATIBLE) is True
    assert supports_embeddings(Provider.GOOGLE) is True
    assert supports_embeddings(Provider.ANTHROPIC) is False


def test_build_embedding_provider_rejects_anthropic() -> None:
    with pytest.raises(EmbeddingsNotSupported):
        build_embedding_provider(Provider.ANTHROPIC, api_key="x", base_url=None)


def test_build_embedding_provider_returns_a_working_adapter_for_openai() -> None:
    provider = build_embedding_provider(Provider.OPENAI, api_key="x", base_url=None)
    assert isinstance(provider, OpenAICompatibleProvider)
