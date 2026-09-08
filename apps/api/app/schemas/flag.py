"""Request and response shapes for flag definitions, their overrides, and evaluation."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.models import FlagScope, FlagType


class FlagCreate(BaseModel):
    """What defining a new flag needs. Keys are lowercase, dotted identifiers (`provider.openai`,
    `attachments`) — the stable name every override and route gate refers to."""

    key: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9_.\-]+$")
    description: str = Field(min_length=1, max_length=500)
    default_value: Any
    rollout_percentage: int | None = Field(default=None, ge=0, le=100)


class FlagUpdate(BaseModel):
    """A partial edit — only fields present in the request are changed (see services/flags.py)."""

    description: str | None = None
    default_value: Any | None = None
    rollout_percentage: int | None = None
    archived: bool | None = None


class FlagOut(BaseModel):
    """A flag definition as the admin UI and `/flags/evaluate` both see it."""

    id: uuid.UUID
    key: str
    description: str
    type: FlagType
    default_value: Any
    rollout_percentage: int | None
    archived_at: datetime | None
    created_at: datetime


class OverrideCreate(BaseModel):
    """Pin a flag to a value for one user or one workspace."""

    scope: FlagScope
    scope_id: uuid.UUID
    value: Any


class OverrideOut(BaseModel):
    """One targeting rule on a flag."""

    id: uuid.UUID
    flag_id: uuid.UUID
    scope: FlagScope
    scope_id: uuid.UUID
    value: Any
    created_at: datetime
