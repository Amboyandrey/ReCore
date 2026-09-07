"""The registry hands back the right adapter type for each provider — the one branch point."""

from app.models.provider import Provider
from app.providers.anthropic import AnthropicProvider
from app.providers.google import GoogleProvider
from app.providers.openai_compatible import OpenAICompatibleProvider
from app.providers.registry import build_provider


def test_each_provider_maps_to_its_adapter() -> None:
    """Anthropic and Google get dedicated adapters; OpenAI and the generic case share one."""
    assert isinstance(build_provider(Provider.ANTHROPIC, api_key="k", base_url=None), AnthropicProvider)
    assert isinstance(build_provider(Provider.GOOGLE, api_key="k", base_url=None), GoogleProvider)
    assert isinstance(
        build_provider(Provider.OPENAI, api_key="k", base_url=None), OpenAICompatibleProvider
    )
    assert isinstance(
        build_provider(Provider.OPENAI_COMPATIBLE, api_key="k", base_url="http://localhost:11434/v1"),
        OpenAICompatibleProvider,
    )
