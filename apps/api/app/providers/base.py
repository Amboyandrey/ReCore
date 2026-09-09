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
class ImagePart:
    """One image sent alongside a turn's text.

    Raw bytes, not pre-encoded — each adapter base64-encodes into its own wire format, rather
    than every caller (and every history-replay path) guessing what a specific provider wants.
    """

    mime: str
    data: bytes


@dataclass(frozen=True)
class ToolDefinition:
    """One tool the model is told it may call — offered on every stream() call that has any.

    `parameters` is a JSON Schema object (the same shape every provider's function-calling API
    already wants), so callers build it once rather than each adapter inventing its own.
    """

    name: str
    description: str
    parameters: dict[str, object]


@dataclass(frozen=True)
class ToolCall:
    """One invocation the model asked for. `id` is the provider's own call id — echoed back on
    the `role="tool"` message that answers it, so the model can match results to requests when it
    asked for more than one call in the same turn."""

    id: str
    name: str
    arguments: dict[str, object]


@dataclass(frozen=True)
class ChatMessage:
    """One turn to send to a provider.

    `tool_calls` is set on an `assistant` turn being replayed that asked for tools — the model
    needs to see its own past request, not just the answer, to make sense of the `tool` turn that
    follows it. `tool_call_id` and `tool_name` are set on a `tool` turn: OpenAI and Anthropic
    match a result back to its request by id; Google's API has no call id at all and matches by
    name instead, so a tool turn carries both rather than forcing Google's adapter to thread the
    name through from the assistant turn several messages back.
    """

    role: Literal["user", "assistant", "system", "tool"]
    content: str
    images: tuple[ImagePart, ...] = ()
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    tool_name: str | None = None

    def __post_init__(self) -> None:
        """Every one of these is a real constraint from Anthropic/OpenAI/Google's own APIs,
        enforced here once so no adapter has to defend against illegal input itself."""
        if self.images and self.role != "user":
            raise ValueError("Only user turns can carry images.")
        if self.tool_calls and self.role != "assistant":
            raise ValueError("Only assistant turns can carry tool_calls.")
        if self.role == "tool" and (self.tool_call_id is None or self.tool_name is None):
            raise ValueError("A tool turn must set both tool_call_id and tool_name.")
        if self.role != "tool" and (self.tool_call_id is not None or self.tool_name is not None):
            raise ValueError("Only a tool turn can set tool_call_id or tool_name.")


@dataclass(frozen=True)
class TextDelta:
    """A piece of the assistant's reply as it streams in."""

    text: str


@dataclass(frozen=True)
class ToolCallRequest:
    """The model wants one or more tools called before it continues. Adapters accumulate whatever
    fragments their own wire format streams the call in as (OpenAI: indexed deltas; Anthropic:
    a content block plus incremental JSON; Google: a single functionCall part) and emit exactly
    one of these, fully assembled, rather than pushing partial-call bookkeeping onto the loop that
    consumes chunks."""

    calls: tuple[ToolCall, ...]


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
Chunk = TextDelta | ToolCallRequest | Usage | Done | StreamError


class LLMProvider(Protocol):
    """What every provider adapter must implement."""

    async def validate(self) -> CredentialCheck:
        """Confirm the credential actually works against the provider."""
        ...

    async def list_models(self) -> list[ModelInfo]:
        """Fetch and normalize the provider's list of available models."""
        ...

    def stream(
        self,
        *,
        model: str,
        messages: Sequence[ChatMessage],
        max_tokens: int,
        tools: Sequence[ToolDefinition] = (),
    ) -> AsyncIterator[Chunk]:
        """Stream a chat completion, yielding normalized chunks as they arrive.

        `tools` defaults to empty, and every adapter must leave its payload exactly as it is
        today when no tools are offered — some OpenAI-compatible endpoints reject an empty or
        unexpected `tools` field outright, the same reasoning `_content()`'s bare-string case
        already follows for images.
        """
        ...
