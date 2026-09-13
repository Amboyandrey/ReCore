"""One knowledge chunk that was actually folded into a reply's prompt — what lets the chat UI's
sources sidebar show, per message, exactly what was used (see PR 2 / app/services/chat.py)."""

import uuid

from sqlalchemy import Float, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class MessageSource(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "message_sources"

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    message_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"))
    # Kept even if the connector or document is later deleted (SET NULL) — the label/url/snippet
    # below are denormalized so a message's own history stays legible regardless.
    connector_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("connectors.id", ondelete="SET NULL"), default=None
    )
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("connector_documents.id", ondelete="SET NULL"), default=None
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    label: Mapped[str]
    url: Mapped[str | None] = mapped_column(default=None)
    snippet: Mapped[str]
    score: Mapped[float] = mapped_column(Float)
