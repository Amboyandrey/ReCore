"""Request and response shapes for provider credentials. The plaintext key never appears in a
response — only in the create request, which the service validates and encrypts immediately."""

import uuid
from datetime import datetime
from typing import Self

from pydantic import BaseModel, Field, model_validator

from app.models import Provider


class CredentialCreate(BaseModel):
    """What registering a credential needs. `base_url` is required for openai_compatible,
    optional (an advanced override) for the three named providers."""

    provider: Provider
    label: str = Field(min_length=1, max_length=100)
    api_key: str = Field(min_length=1)
    base_url: str | None = None

    @model_validator(mode="after")
    def _require_base_url_for_openai_compatible(self) -> Self:
        """An openai_compatible credential is meaningless without knowing where to send requests."""
        if self.provider == Provider.OPENAI_COMPATIBLE and not self.base_url:
            raise ValueError("base_url is required for an OpenAI-compatible provider.")
        return self


class CredentialOut(BaseModel):
    """A credential as seen after creation — shape and metadata only, never the key itself."""

    id: uuid.UUID
    provider: Provider
    label: str
    base_url: str | None
    last4: str
    disabled_at: datetime | None
    created_at: datetime
