"""Workspace, membership, and invitation rows persist with the constraints the schema promises."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Invitation, Role, User, Workspace, WorkspaceMember
from app.models.role import role_at_least


async def _make_user(db_session: AsyncSession, email: str) -> User:
    user = User(email=email, password_hash="hashed")
    db_session.add(user)
    await db_session.flush()
    return user


async def test_workspace_round_trips(db_session: AsyncSession) -> None:
    """A saved workspace comes back with its defaults applied."""
    owner = await _make_user(db_session, "owner@example.com")
    workspace = Workspace(slug="acme", name="Acme", owner_id=owner.id)
    db_session.add(workspace)
    await db_session.flush()

    assert workspace.id is not None
    assert workspace.settings == {}
    assert workspace.deleted_at is None


async def test_slug_uniqueness_is_enforced(db_session: AsyncSession) -> None:
    """Two workspaces can't share a slug."""
    owner = await _make_user(db_session, "owner2@example.com")
    db_session.add(Workspace(slug="acme", name="Acme", owner_id=owner.id))
    await db_session.flush()

    db_session.add(Workspace(slug="acme", name="Acme Again", owner_id=owner.id))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_membership_composite_key_is_unique(db_session: AsyncSession) -> None:
    """One user can only hold one membership row per workspace."""
    owner = await _make_user(db_session, "owner3@example.com")
    workspace = Workspace(slug="acme3", name="Acme", owner_id=owner.id)
    db_session.add(workspace)
    await db_session.flush()

    db_session.add(WorkspaceMember(workspace_id=workspace.id, user_id=owner.id, role=Role.OWNER))
    await db_session.flush()

    db_session.add(WorkspaceMember(workspace_id=workspace.id, user_id=owner.id, role=Role.MEMBER))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_invitation_round_trips(db_session: AsyncSession) -> None:
    """An invitation persists with its role and expiry, unaccepted by default."""
    owner = await _make_user(db_session, "owner4@example.com")
    workspace = Workspace(slug="acme4", name="Acme", owner_id=owner.id)
    db_session.add(workspace)
    await db_session.flush()

    invite = Invitation(
        workspace_id=workspace.id,
        email="new@example.com",
        role=Role.MEMBER,
        token_hash="hashed-token",
        invited_by=owner.id,
        expires_at=datetime.now(UTC) + timedelta(days=3),
    )
    db_session.add(invite)
    await db_session.flush()

    assert invite.accepted_at is None
    assert invite.role == Role.MEMBER


def test_role_ordering() -> None:
    """The hierarchy compares the way require_role will rely on: viewer < member < admin < owner."""
    assert role_at_least(Role.OWNER, Role.VIEWER) is True
    assert role_at_least(Role.VIEWER, Role.OWNER) is False
    assert role_at_least(Role.ADMIN, Role.ADMIN) is True
    assert role_at_least(Role.MEMBER, Role.ADMIN) is False
