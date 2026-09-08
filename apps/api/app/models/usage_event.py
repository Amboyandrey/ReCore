"""An append-only record of one generation's cost — never updated, only ever inserted.

Message already carries its own tokens/cost for the conversation view; this table exists so the
usage dashboard can aggregate across a workspace without scanning every conversation's messages.
"""

import uuid

from sqlalchemy import Enum, ForeignKey, Integer, Numeric
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.models.provider import Provider


class UsageEvent(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One priced generation — one row per assistant reply, win or fail."""

    __tablename__ = "usage_events"

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE")
    )
    message_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"))
    model_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("models.id", ondelete="CASCADE"))
    # Reuses the "provider" enum type created for provider_credentials — no new type here.
    provider: Mapped[Provider] = mapped_column(Enum(Provider, name="provider"))
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Numeric(10, 6, asdecimal=False), default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
