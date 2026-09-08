"""The audit log viewer — owner only, per ARCHITECTURE.md's route table."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.deps.workspace import WorkspaceCtx, require_role
from app.models import Role
from app.schemas.audit import AuditLogOut
from app.services.audit import list_audit_logs

router = APIRouter(prefix="/workspaces/{workspace_id}/audit-logs", tags=["audit"])


@router.get("", response_model=list[AuditLogOut])
async def list_audit_logs_route(
    ctx: WorkspaceCtx = Depends(require_role(Role.OWNER)),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
    before: str | None = Query(default=None),
) -> list[AuditLogOut]:
    """List the workspace's audit trail, newest first, cursor-paginated via `before`."""
    logs = await list_audit_logs(db, workspace_id=ctx.workspace_id, limit=limit, before=before)
    return [AuditLogOut.model_validate(log, from_attributes=True) for log in logs]
