"""A deterministic provider used by tests — no network calls, a fixed key that "works"."""

from app.providers.base import CredentialCheck, ModelInfo

VALID_KEY = "fake-valid-key"


class FakeProvider:
    """Validates exactly one hardcoded key; everything else fails, deterministically."""

    def __init__(self, api_key: str, base_url: str | None = None) -> None:
        del base_url  # unused — the fake has nowhere to send requests
        self._valid = api_key == VALID_KEY

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
