"""A workspace's own mem0.ai API key — envelope-encrypted like every other secret credential in
this codebase (see app/core/crypto.py)."""

import uuid

from sqlalchemy import ForeignKey, LargeBinary
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.mixins import TimestampMixin


class MemoryCredential(Base, TimestampMixin):
    """The workspace's mem0 key. Exactly one per workspace — `workspace_id` is this table's own
    primary key rather than a separate surrogate one, since there's never a reason to hold two."""

    __tablename__ = "memory_credentials"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    nonce: Mapped[bytes] = mapped_column(LargeBinary)
    wrapped_key: Mapped[bytes] = mapped_column(LargeBinary)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
