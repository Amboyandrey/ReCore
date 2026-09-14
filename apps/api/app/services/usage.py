"""Recording the cost of every generation, append-only, and aggregating it for the dashboard.

ARCHITECTURE.md calls for nightly rollups into daily aggregates once this gets slow — at this
scale, aggregating `usage_events` directly on read is simpler and just as correct; a rollup table
is a straightforward addition later without changing what these functions return.
"""

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import LLMModel, Provider, UsageEvent, User


async def record_usage_event(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID | None = None,
    message_id: uuid.UUID | None = None,
    workflow_run_id: uuid.UUID | None = None,
    model_id: uuid.UUID,
    provider: Provider,
    tokens_in: int,
    tokens_out: int,
    cost_usd: float,
    latency_ms: int,
) -> UsageEvent:
    """Append one priced generation to the ledger — called once, right after the reply (or
    workflow step) lands. Exactly one of `conversation_id`/`message_id` (a chat reply) or
    `workflow_run_id` (a workflow step) is expected — see UsageEvent's own docstring."""
    event = UsageEvent(
        workspace_id=workspace_id,
        user_id=user_id,
        conversation_id=conversation_id,
        message_id=message_id,
        workflow_run_id=workflow_run_id,
        model_id=model_id,
        provider=provider,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost_usd,
        latency_ms=latency_ms,
    )
    db.add(event)
    await db.flush()
    return event


async def usage_by_model(
    db: AsyncSession, *, workspace_id: uuid.UUID, since: datetime
) -> list[dict[str, object]]:
    """Tokens, cost, and message count per model, in the window, priciest first."""
    stmt = (
        select(
            UsageEvent.model_id,
            LLMModel.display_name,
            func.sum(UsageEvent.tokens_in),
            func.sum(UsageEvent.tokens_out),
            func.sum(UsageEvent.cost_usd),
            func.count(UsageEvent.id),
        )
        .join(LLMModel, LLMModel.id == UsageEvent.model_id)
        .where(UsageEvent.workspace_id == workspace_id, UsageEvent.created_at >= since)
        .group_by(UsageEvent.model_id, LLMModel.display_name)
        .order_by(func.sum(UsageEvent.cost_usd).desc())
    )
    rows = (await db.execute(stmt)).all()
    return [
        {
            "model_id": model_id,
            "display_name": display_name,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost_usd": float(cost_usd),
            "message_count": message_count,
        }
        for model_id, display_name, tokens_in, tokens_out, cost_usd, message_count in rows
    ]


async def usage_by_member(
    db: AsyncSession, *, workspace_id: uuid.UUID, since: datetime
) -> list[dict[str, object]]:
    """Tokens, cost, and message count per member, in the window, priciest first."""
    stmt = (
        select(
            UsageEvent.user_id,
            User.email,
            func.sum(UsageEvent.tokens_in),
            func.sum(UsageEvent.tokens_out),
            func.sum(UsageEvent.cost_usd),
            func.count(UsageEvent.id),
        )
        .join(User, User.id == UsageEvent.user_id)
        .where(UsageEvent.workspace_id == workspace_id, UsageEvent.created_at >= since)
        .group_by(UsageEvent.user_id, User.email)
        .order_by(func.sum(UsageEvent.cost_usd).desc())
    )
    rows = (await db.execute(stmt)).all()
    return [
        {
            "user_id": user_id,
            "email": email,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost_usd": float(cost_usd),
            "message_count": message_count,
        }
        for user_id, email, tokens_in, tokens_out, cost_usd, message_count in rows
    ]


async def usage_by_day(
    db: AsyncSession, *, workspace_id: uuid.UUID, since: datetime
) -> list[dict[str, object]]:
    """Tokens, cost, and message count per day, in the window, oldest first."""
    day = func.date_trunc("day", UsageEvent.created_at)
    stmt = (
        select(
            day,
            func.sum(UsageEvent.tokens_in),
            func.sum(UsageEvent.tokens_out),
            func.sum(UsageEvent.cost_usd),
            func.count(UsageEvent.id),
        )
        .where(UsageEvent.workspace_id == workspace_id, UsageEvent.created_at >= since)
        .group_by(day)
        .order_by(day)
    )
    rows = (await db.execute(stmt)).all()
    return [
        {
            "day": day_value,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost_usd": float(cost_usd),
            "message_count": message_count,
        }
        for day_value, tokens_in, tokens_out, cost_usd, message_count in rows
    ]
