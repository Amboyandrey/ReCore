"""Listing, promoting/demoting, and removing workspace members — with the last-owner invariant."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import InsufficientRole, LastOwnerError, MemberNotFound
from app.models import Role, User, WorkspaceMember


async def list_members(db: AsyncSession, *, workspace_id: uuid.UUID) -> list[tuple[WorkspaceMember, User]]:
    """List every member of a workspace with their user record, in the order they joined."""
    stmt = (
        select(WorkspaceMember, User)
        .join(User, User.id == WorkspaceMember.user_id)
        .where(WorkspaceMember.workspace_id == workspace_id)
        .order_by(WorkspaceMember.joined_at)
    )
    result = await db.execute(stmt)
    return [(member, user) for member, user in result.all()]


async def _owner_count(db: AsyncSession, workspace_id: uuid.UUID) -> int:
    """Count how many owners a workspace currently has."""
    stmt = select(func.count()).select_from(WorkspaceMember).where(
        WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.role == Role.OWNER
    )
    return await db.scalar(stmt) or 0


async def _get_member(db: AsyncSession, workspace_id: uuid.UUID, user_id: uuid.UUID) -> WorkspaceMember:
    """Load a membership row by its composite key, or raise if the target isn't a member."""
    member = await db.get(WorkspaceMember, {"workspace_id": workspace_id, "user_id": user_id})
    if member is None:
        raise MemberNotFound()
    return member


async def change_member_role(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    acting_role: Role,
    target_user_id: uuid.UUID,
    new_role: Role,
) -> WorkspaceMember:
    """Change a member's role — only an owner may touch the owner role, and one must always remain."""
    member = await _get_member(db, workspace_id, target_user_id)
    if (member.role == Role.OWNER or new_role == Role.OWNER) and acting_role != Role.OWNER:
        raise InsufficientRole()
    if member.role == Role.OWNER and new_role != Role.OWNER and await _owner_count(db, workspace_id) <= 1:
        raise LastOwnerError()
    member.role = new_role
    await db.flush()
    return member


async def remove_member(
    db: AsyncSession, *, workspace_id: uuid.UUID, acting_role: Role, target_user_id: uuid.UUID
) -> None:
    """Remove a member — only an owner may remove another owner, and one must always remain."""
    member = await _get_member(db, workspace_id, target_user_id)
    if member.role == Role.OWNER and acting_role != Role.OWNER:
        raise InsufficientRole()
    if member.role == Role.OWNER and await _owner_count(db, workspace_id) <= 1:
        raise LastOwnerError()
    await db.delete(member)
    await db.flush()
