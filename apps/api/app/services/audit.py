"""Writing and reading the immutable audit trail — one row per privileged action."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import InvalidCursor
from app.models import AuditLog


async def record_audit(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID,
    workspace_id: uuid.UUID | None,
    action: str,
    target_type: str,
    target_id: str,
    ip: str,
    metadata: dict[str, Any] | None = None,
) -> AuditLog:
    """Append one row to the audit trail. Callers never update or delete it afterward."""
    entry = AuditLog(
        actor_id=actor_id,
        workspace_id=workspace_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        ip=ip,
        event_metadata=metadata,
    )
    db.add(entry)
    await db.flush()
    return entry


async def list_audit_logs(
    db: AsyncSession, *, workspace_id: uuid.UUID, limit: int, before: str | None
) -> list[AuditLog]:
    """List a workspace's audit trail, newest first, cursor-paginated on `created_at` via `before`.

    `before` is an ISO timestamp from a previous page's last row — deep pages cost the same as
    the first one, since it's a plain indexed range scan, not an OFFSET.
    """
    stmt = select(AuditLog).where(AuditLog.workspace_id == workspace_id)
    if before is not None:
        try:
            cursor = datetime.fromisoformat(before)
        except ValueError as exc:
            raise InvalidCursor() from exc
        stmt = stmt.where(AuditLog.created_at < cursor)
    stmt = stmt.order_by(AuditLog.created_at.desc()).limit(limit)
    return list((await db.scalars(stmt)).all())
