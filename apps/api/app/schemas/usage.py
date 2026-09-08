"""Response shapes for the usage dashboard."""

import uuid
from datetime import datetime

from pydantic import BaseModel


class UsageByModel(BaseModel):
    """One model's totals over the window."""

    model_id: uuid.UUID
    display_name: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    message_count: int


class UsageByMember(BaseModel):
    """One member's totals over the window."""

    user_id: uuid.UUID
    email: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    message_count: int


class UsageByDay(BaseModel):
    """One day's totals over the window."""

    day: datetime
    tokens_in: int
    tokens_out: int
    cost_usd: float
    message_count: int


class UsageSummary(BaseModel):
    """Everything the usage dashboard needs in one call: spend broken down three ways."""

    by_model: list[UsageByModel]
    by_member: list[UsageByMember]
    by_day: list[UsageByDay]
