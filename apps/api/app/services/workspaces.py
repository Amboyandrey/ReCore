"""Creating and listing workspaces — the membership dependency chain lives in app/deps/workspace.py."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.slugify import slugify
from app.models import Role, User, Workspace, WorkspaceMember


async def _unique_slug(db: AsyncSession, base: str) -> str:
    """Find a slug that isn't taken yet, appending -2, -3, ... to the base if it is."""
    slug = base
    suffix = 1
    while await db.scalar(select(Workspace.id).where(Workspace.slug == slug)) is not None:
        suffix += 1
        slug = f"{base}-{suffix}"
    return slug


async def create_workspace(db: AsyncSession, *, owner: User, name: str) -> Workspace:
    """Create a workspace and make its creator the owner, in one flush."""
    slug = await _unique_slug(db, slugify(name))
    workspace = Workspace(slug=slug, name=name, owner_id=owner.id)
    db.add(workspace)
    await db.flush()
    db.add(WorkspaceMember(workspace_id=workspace.id, user_id=owner.id, role=Role.OWNER))
    await db.flush()
    return workspace


async def list_my_workspaces(db: AsyncSession, *, user: User) -> list[tuple[Workspace, Role]]:
    """Return every workspace the user belongs to, paired with their role in each, newest first."""
    stmt = (
        select(Workspace, WorkspaceMember.role)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
        .where(WorkspaceMember.user_id == user.id, Workspace.deleted_at.is_(None))
        .order_by(Workspace.created_at.desc())
    )
    result = await db.execute(stmt)
    return [(workspace, role) for workspace, role in result.all()]
