"""A saved, ordered chain of assistant steps that runs in the background (see
app/workers/run_workflow.py) — no chat turn, no conversation. Each step's assistant gets a prompt
built from the run's own input and earlier steps' outputs (see app/workflows/template.py)."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class Workflow(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One workspace's saved workflow definition. `default_model_id` backs any step whose own
    assistant has no preferred model, the same role a conversation's model plays for an assistant
    in chat — a step that resolves neither is rejected at save time (see services/workflows.py).

    `schedule_cron`/`schedule_timezone`/`next_run_at` and `webhook_secret` are created now (even
    though only PR 1's manual trigger reads them) so a later PR needs no second migration.
    """

    __tablename__ = "workflows"

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    name: Mapped[str]
    description: Mapped[str] = mapped_column(String, default="", server_default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    default_model_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("models.id", ondelete="SET NULL"), default=None
    )
    # A standard 5-field cron expression; None means "no schedule". Validated (and next_run_at
    # computed) with croniter in services/workflows.py, not here.
    schedule_cron: Mapped[str | None] = mapped_column(default=None)
    schedule_timezone: Mapped[str] = mapped_column(default="UTC", server_default="UTC")
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # None means the inbound webhook trigger is disabled for this workflow — see
    # routers/v1/hooks.py. The URL embeds it directly, so it's the credential, not just an id.
    webhook_secret: Mapped[str | None] = mapped_column(unique=True, default=None)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
