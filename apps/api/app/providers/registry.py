"""Maps a Provider enum value to its adapter — the one place that knows about all four."""

from collections.abc import Callable

from app.models.provider import Provider
from app.providers.anthropic import AnthropicProvider
from app.providers.base import LLMProvider
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
