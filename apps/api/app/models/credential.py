"""A workspace's own API key for one LLM provider — the plaintext never touches this table."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, LargeBinary
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.models.provider import Provider


class ProviderCredential(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """An envelope-encrypted API key registered for one workspace, plus its display metadata.

    `ciphertext`/`nonce` are the secret encrypted under a random per-credential data key;
    `wrapped_key` is that data key encrypted under the app's master key (see app/core/crypto.py).
    """

    __tablename__ = "provider_credentials"

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    provider: Mapped[Provider] = mapped_column(Enum(Provider, name="provider"))
    label: Mapped[str]
    base_url: Mapped[str | None] = mapped_column(default=None)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    nonce: Mapped[bytes] = mapped_column(LargeBinary)
    wrapped_key: Mapped[bytes] = mapped_column(LargeBinary)
    last4: Mapped[str]
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
