"""The interface every LLM provider adapter implements — one protocol, no branching by caller."""

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol


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


@dataclass(frozen=True)
class ChatMessage:
    """One turn to send to a provider — a plain role and its text content."""

    role: Literal["user", "assistant", "system"]
    content: str


@dataclass(frozen=True)
class TextDelta:
    """A piece of the assistant's reply as it streams in."""

    text: str


@dataclass(frozen=True)
class Usage:
    """Token counts reported once a generation finishes."""

    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class Done:
    """Marks the end of a generation, successful or not."""

    finish_reason: str


@dataclass(frozen=True)
class StreamError:
    """A provider-side failure encountered mid-stream, as opposed to a validation failure."""

    message: str


# A closed union — adapters translate their own wire format into this, callers never branch on
# which provider produced a chunk.
Chunk = TextDelta | Usage | Done | StreamError


class LLMProvider(Protocol):
    """What every provider adapter must implement."""

    async def validate(self) -> CredentialCheck:
        """Confirm the credential actually works against the provider."""
        ...

    async def list_models(self) -> list[ModelInfo]:
        """Fetch and normalize the provider's list of available models."""
        ...

    def stream(
        self, *, model: str, messages: Sequence[ChatMessage], max_tokens: int
    ) -> AsyncIterator[Chunk]:
        """Stream a chat completion, yielding normalized chunks as they arrive."""
        ...
