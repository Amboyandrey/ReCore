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
    """One priced generation — one row per assistant reply or workflow step run, win or fail.

    `conversation_id`/`message_id` are set for a chat reply and null for a workflow step (there is
    no conversation or message to point at); `workflow_run_id` is the reverse — set only for a
    step. Exactly one of the two pairs is populated, but that isn't enforced at the database level
    (a CHECK constraint here would outlive its usefulness the moment a third billable source
    shows up) — every caller (record_usage_event's two call sites) passes exactly one.
    """

    __tablename__ = "usage_events"

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), default=None
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), default=None
    )
    workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="CASCADE"), default=None
    )
    model_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("models.id", ondelete="CASCADE"))
    # Reuses the "provider" enum type created for provider_credentials — no new type here.
    provider: Mapped[Provider] = mapped_column(Enum(Provider, name="provider"))
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Numeric(10, 6, asdecimal=False), default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
