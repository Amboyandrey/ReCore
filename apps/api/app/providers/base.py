"""The interface every LLM provider adapter implements — one protocol, no branching by caller.

Streaming chat completions (`stream()`) land in phase 5; for now every adapter only needs to
prove a key works and report what models it can see, which is what registering a credential
actually requires.
"""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ModelInfo:
    """One model a provider's API reports as available."""

    id: str
    display_name: str
    context_window: int | None = None


@dataclass(frozen=True)
class CredentialCheck:
    """The result of validating a credential against its provider."""

    ok: bool
    detail: str


class LLMProvider(Protocol):
    """What every provider adapter must implement."""

    async def validate(self) -> CredentialCheck:
        """Confirm the credential actually works against the provider."""
        ...

    async def list_models(self) -> list[ModelInfo]:
        """Fetch and normalize the provider's list of available models."""
        ...
