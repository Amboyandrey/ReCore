"""A deterministic provider used by tests — no network calls, a fixed key that "works"."""

from collections.abc import AsyncIterator, Sequence

from app.providers.base import ChatMessage, Chunk, CredentialCheck, Done, ModelInfo, TextDelta, Usage

VALID_KEY = "fake-valid-key"

# What every non-empty stream() call yields, split into words so tests can assert on more than
# one delta arriving — a single-chunk reply wouldn't exercise anything about streaming at all.
FAKE_REPLY = "Hello from the fake provider."


class FakeProvider:
    """Validates exactly one hardcoded key; everything else fails, deterministically."""

    def __init__(self, api_key: str, base_url: str | None = None) -> None:
        del base_url  # unused — the fake has nowhere to send requests
        self._valid = api_key == VALID_KEY
        # Set by stream() on every call — lets a test assert on what it was actually handed
        # (images included) without needing a real network call to inspect.
        self.last_messages: Sequence[ChatMessage] = ()

    async def validate(self) -> CredentialCheck:
        """Report success only for the one key this fake recognizes."""
        if self._valid:
            return CredentialCheck(ok=True, detail="Key validated.")
        return CredentialCheck(ok=False, detail="Invalid API key.")

    async def list_models(self) -> list[ModelInfo]:
        """Return a fixed, small model list — only for the recognized key."""
        if not self._valid:
            raise RuntimeError("Cannot list models for an invalid credential.")
        return [
            ModelInfo(id="fake-small", display_name="Fake Small", context_window=8_000),
            ModelInfo(id="fake-large", display_name="Fake Large", context_window=200_000),
        ]

    async def stream(
        self, *, model: str, messages: Sequence[ChatMessage], max_tokens: int
    ) -> AsyncIterator[Chunk]:
        """Yield a fixed reply word by word, then usage and a normal finish."""
        del model, max_tokens
        self.last_messages = messages
        input_tokens = sum(len(m.content.split()) for m in messages)
        for word in FAKE_REPLY.split(" "):
            yield TextDelta(text=word + " ")
        yield Usage(input_tokens=input_tokens, output_tokens=len(FAKE_REPLY.split(" ")))
        yield Done(finish_reason="stop")
