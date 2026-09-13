"""Maps a Provider enum value to its adapter — the one place that knows about all four."""

from collections.abc import Callable

from app.core.errors import EmbeddingsNotSupported
from app.models.provider import Provider
from app.providers.anthropic import AnthropicProvider
from app.providers.base import EmbeddingProvider, LLMProvider
from app.providers.google import GoogleProvider
from app.providers.openai_compatible import OpenAICompatibleProvider

# LLMProvider is a Protocol with no __init__, so the value type here is a constructor callable
# (any of these classes, called with these two keyword args) rather than type[LLMProvider].
_Factory = Callable[..., LLMProvider]

# OpenAI's own API is exactly the shape OpenAICompatibleProvider already speaks, so it needs no
# adapter of its own — that's also what makes generic OpenAI-compatible endpoints work at all.
_ADAPTERS: dict[Provider, _Factory] = {
    Provider.ANTHROPIC: AnthropicProvider,
    Provider.OPENAI: OpenAICompatibleProvider,
    Provider.GOOGLE: GoogleProvider,
    Provider.OPENAI_COMPATIBLE: OpenAICompatibleProvider,
}


def build_provider(provider: Provider, *, api_key: str, base_url: str | None) -> LLMProvider:
    """Instantiate the adapter for a given provider, wired with its credential."""
    return _ADAPTERS[provider](api_key=api_key, base_url=base_url)


# Anthropic has no embeddings API — every other provider's adapter (all built on the same
# OpenAI-compatible or Google shape) implements EmbeddingProvider.embed.
_EMBEDDING_PROVIDERS = {Provider.OPENAI, Provider.OPENAI_COMPATIBLE, Provider.GOOGLE}


def supports_embeddings(provider: Provider) -> bool:
    return provider in _EMBEDDING_PROVIDERS


def build_embedding_provider(
    provider: Provider, *, api_key: str, base_url: str | None
) -> EmbeddingProvider:
    """Instantiate an embeddings-capable adapter, or raise if this provider has none."""
    if not supports_embeddings(provider):
        raise EmbeddingsNotSupported()
    # Every _EMBEDDING_PROVIDERS entry's adapter class also implements EmbeddingProvider —
    # asserted by the membership check above rather than a separate registry, since it's the
    # same classes as _ADAPTERS.
    return _ADAPTERS[provider](api_key=api_key, base_url=base_url)  # type: ignore[return-value]
