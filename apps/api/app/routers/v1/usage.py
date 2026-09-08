"""The usage dashboard: tokens and spend by model, member, and day."""

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.deps.workspace import WorkspaceCtx, require_role
from app.models import Role
from app.schemas.usage import UsageByDay, UsageByMember, UsageByModel, UsageSummary
from app.services.usage import usage_by_day, usage_by_member, usage_by_model

router = APIRouter(prefix="/workspaces/{workspace_id}/usage", tags=["usage"])


@router.get("", response_model=UsageSummary)
async def get_usage_route(
    ctx: WorkspaceCtx = Depends(require_role(Role.ADMIN)),
    db: AsyncSession = Depends(get_db),
    days: int = Query(default=30, ge=1, le=365),
) -> UsageSummary:
    """Spend and token counts by model, member, and day, over the trailing `days`."""
    since = datetime.now(UTC) - timedelta(days=days)
    return UsageSummary(
        by_model=[
            UsageByModel(**row)
            for row in await usage_by_model(db, workspace_id=ctx.workspace_id, since=since)
        ],
        by_member=[
            UsageByMember(**row)
            for row in await usage_by_member(db, workspace_id=ctx.workspace_id, since=since)
        ],
        by_day=[
            UsageByDay(**row) for row in await usage_by_day(db, workspace_id=ctx.workspace_id, since=since)
        ],
    )
